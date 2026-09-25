"""The operator tool that upholds a dispute, pays it and rates the agent (`scripts/uphold_dispute.py`).

Stories 4.03 and 4.04 end in two transactions a grant reviewer can open on
Stellar Expert — the credit and the dispute rating — and CI can produce
neither, because both need the funded settler key and that key never reaches a
CI runner. What CI *can* do is make the tool safe to point at real money at
2am, and that is the whole of this file. The properties, in the order they
matter:

  - the preview signs NOTHING — the stellar client is not reached at all, not
    even for a read;
  - every refusal exits non-zero, with its own code and a sentence saying why;
  - a timed-out transfer is reported as "may still land, do not re-run", never
    as a failure, because the opposite reading credits the buyer twice;
  - a rating is reported as landed only when the ledger vouched for it during
    the run, and a paid dispute whose rating did not land says the OPPOSITE of
    the line above — re-running is safe — on a code of its own;
  - no line of output can carry a secret.

Hermetic: the in-memory dispute store, a stubbed refund service, and either a
stubbed uphold or — wherever the rating is under test — the REAL one with only
the transfer and the ledger's answer replaced, because the rating's verdict is
read off a record the adjudication service writes. Nothing here touches the
chain, a database or the network — and the stellar client is deliberately
booby-trapped, so "it never signs" is checked rather than asserted in a
docstring.

`refund_svc.creditable_for` (with `RefundRefused`) and `dispute_svc.uphold` land
on sibling lanes of this same story. What is pinned here is what THIS script
does with each answer those two calls can give, which is what the script owns
and what a merge cannot change silently. The seams bind to the real types when
the modules already carry them and to stand-ins with the same surface when they
do not, so the file holds either side of that merge.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Any

import pytest

import app.stellar.client as sc
from app.config import settings
from app.services import dispute_rating, dispute_store, dispute_svc, refund_svc, reputation_svc
from app.services.dispute_store import DisputeRecord, DisputeStatus, SettlementRecord, SettlementStep
from scripts import uphold_dispute

DISPUTE_ID = "dsp_0123456789abcdef"
JOB = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
TASK = "tsk_disputed"
PAYER = "GBUYER7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R"
AGENT = "agt_seo"
STEPS = (
    SettlementStep(step_index=0, agent_id="agt_writer", agent_name="Copywriter", price_usdc=0.05, delivered=True),
    SettlementStep(step_index=1, agent_id=AGENT, agent_name="SEO Brief", price_usdc=0.07, delivered=True),
)
STEP_INDEX = 1
SETTLED_USDC = 0.12
CREDITABLE_USDC = 0.07
REFUND_TX = "b7c1d2e3f405162738495a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f809"
LEDGER = "C" + "LEDGER7Q" * 6 + "ABCDEFG"
RATING_TX = "d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3"


class _Refused(Exception):
    """Stand-in for `refund_svc.RefundRefused` while the refund lane is in flight.

    Carries only the two attributes the script reads — `code` and `message` —
    because binding to more would be binding to an implementation this file does
    not own.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CreditSeam:
    """How `refund_svc.creditable_for` answers, for one test.

    Bound rather than imported so these tests pin what the SCRIPT does with each
    answer — which line it prints, which exit code it picks — instead of
    re-testing D4's arithmetic, which the refund lane covers where it lives. It
    binds to the service's real `RefundRefused` the moment that module carries
    one, so nothing here quietly stops testing the real thing after the merge.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.refused_type: type[Exception] = getattr(refund_svc, "RefundRefused", _Refused)
        monkeypatch.setattr(refund_svc, "RefundRefused", self.refused_type, raising=False)
        self.pays(CREDITABLE_USDC)

    def pays(self, amount: float) -> None:
        self._monkeypatch.setattr(refund_svc, "creditable_for", lambda *_a, **_k: amount, raising=False)

    def refuses(self, code: str, message: str = "the refund service refused") -> None:
        def _raise(*_args: Any, **_kwargs: Any) -> float:
            raise self.refused_type(code, message)

        self._monkeypatch.setattr(refund_svc, "creditable_for", _raise, raising=False)


@pytest.fixture
def credit(monkeypatch: pytest.MonkeyPatch) -> CreditSeam:
    """The refund service's D4 answer, paying the full step credit by default."""
    return CreditSeam(monkeypatch)


def forbid_uphold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything calls `dispute_svc.uphold`.

    Used on every path that must stop before signing. Asserting the refusal's
    exit code alone would pass just as happily on a script that refused loudly
    and paid anyway.
    """

    async def _never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("uphold was called on a path that must never sign")

    monkeypatch.setattr(dispute_svc, "uphold", _never, raising=False)


@pytest.fixture(autouse=True)
def _fresh_store():
    """A store per test.

    It is a process singleton, and `DATABASE_URL` in the environment of whoever
    runs the suite must not turn one of these into a live query — the hermetic
    fixture in conftest does not clear that one.
    """
    saved = settings.database_url, settings.stellar_network
    settings.database_url = ""
    # Pinned so the explorer URLs asserted below are the ones a testnet operator
    # sees, rather than whatever network a developer's .env happens to name.
    settings.stellar_network = "testnet"
    dispute_store._store = None
    yield
    dispute_store._store = None
    settings.database_url, settings.stellar_network = saved


@pytest.fixture(autouse=True)
def chain_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Booby-trap the stellar client for every test in this file.

    Every entry point the refund and rating paths could reach is replaced with
    something that fails the test loudly. "The dry run signs nothing" then
    becomes a property the suite enforces rather than a claim: a line added
    later that derives the settler's public key, simulates, or submits is
    caught here instead of on testnet.

    Every call is also RECORDED, and the list is what a test asserts on. The
    raise alone is not enough since 4.04: the reputation read swallows any
    exception into a degraded prior (by design — a dashboard must survive an
    unreachable ledger), so a trap that only raised would be silenced by the
    very code it is meant to catch.
    """
    calls: list[str] = []

    def _trap(name: str) -> Any:
        def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"the stellar client's {name} was called — this path must never reach the chain")

        return _forbidden

    for name in (
        "invoke_with_server_key_async",
        "signer_public_key",
        "submit_rating_async",
        "contract_ids",
        "simulate_read",
    ):
        monkeypatch.setattr(sc, name, _trap(name))
    return calls


def rep(disputed: int, count: int, *, degraded: bool = False) -> reputation_svc.RepInfo:
    """The agent's reputation as `fetch_rep` reports it, reduced to what the script reads.

    `dispute_rate_bps` is the ledger's own formula (disputed * 10_000 / count),
    stated here so a test reads as the numbers an operator would see.
    """
    return reputation_svc.RepInfo(
        agent_id=AGENT,
        smoothed_bps=6_000,
        lower_bound_bps=5_000,
        avg_bps=6_000,
        count=count,
        weight=700_000 * count,
        disputed=disputed,
        dispute_rate_bps=disputed * 10_000 // count if count else 0,
        source="prior" if degraded else "onchain",
        degraded=degraded,
    )


class StandingSeam:
    """What `reputation_svc.fetch_rep` answers, read by read, for one test.

    Stubbed for EVERY test in this file, and unreadable by default — the
    degraded prior an unreachable ledger really produces — so no test reaches
    the real read (whose failures would linger in the process-wide negative
    cache) and every test that wants numbers asks for them. `reads` records
    each call, which is how the dry run is held to reading nothing at all.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reads: list[str] = []
        self._answers: list[reputation_svc.RepInfo | BaseException] = []
        monkeypatch.setattr(reputation_svc, "fetch_rep", self._fetch)

    def reads_as(self, *answers: reputation_svc.RepInfo | BaseException) -> None:
        """Answer the next reads in order: a `RepInfo`, or an exception to raise."""
        self._answers = list(answers)

    async def _fetch(self, agent_id: str) -> reputation_svc.RepInfo:
        self.reads.append(agent_id)
        answer = self._answers.pop(0) if self._answers else rep(0, 0, degraded=True)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def standing(monkeypatch: pytest.MonkeyPatch) -> StandingSeam:
    return StandingSeam(monkeypatch)


def seed(
    *,
    status: DisputeStatus = "open",
    refund_tx: str | None = None,
    rating_tx: str | None = None,
    creditable_usdc: float = CREDITABLE_USDC,
    settled_usdc: float = SETTLED_USDC,
    steps: tuple[SettlementStep, ...] = STEPS,
    step_index: int = STEP_INDEX,
    with_settlement: bool = True,
) -> DisputeRecord:
    """One settled workflow and one dispute of its second step, in the store."""
    now = time.time()
    store = dispute_store.get_dispute_store()
    dispute = DisputeRecord(
        id=DISPUTE_ID,
        job_id_hex=JOB,
        task_id=TASK,
        step_index=step_index,
        agent_id=AGENT,
        payer=PAYER,
        reason="the brief came back empty",
        status=status,
        charged_usdc=0.07,
        creditable_usdc=creditable_usdc,
        opened_at=now,
        refund_tx=refund_tx,
        rating_tx=rating_tx,
    )

    async def _write() -> None:
        if with_settlement:
            await store.record_settlement(
                SettlementRecord(
                    task_id=TASK,
                    payer=PAYER,
                    auth_id_hex="ab" * 16,
                    job_id_hex=JOB,
                    charge_tx="charge_tx",
                    proof_tx="proof_tx",
                    settled_usdc=settled_usdc,
                    steps=steps,
                    settled_at=now,
                    window_closes_at=now + 3600.0,
                )
            )
        await store.open_dispute(dispute)

    asyncio.run(_write())
    return dispute


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    """Run the CLI and hand back its exit code with everything it printed."""
    code = uphold_dispute.main(list(argv))
    return code, capsys.readouterr().out


# ── the CLI surface ────────────────────────────────────────────────────────


def test_dispute_id_is_required() -> None:
    """Nothing runs without one. Argparse's own exit code (2) is deliberately
    outside the refusal table, so a usage mistake cannot be read as a refusal."""
    with pytest.raises(SystemExit) as exc:
        uphold_dispute.main([])
    assert exc.value.code == 2


def test_dry_run_defaults_to_off_and_is_a_flag() -> None:
    """`--dry-run` is opt-IN, and the live run is what an operator gets by
    default — the safety is in reading the preview first, not in a flag that
    could be forgotten in the other direction."""
    parser = uphold_dispute.build_parser()
    assert parser.parse_args(["--dispute-id", DISPUTE_ID]).dry_run is False
    assert parser.parse_args(["--dispute-id", DISPUTE_ID, "--dry-run"]).dry_run is True


def test_help_says_the_platform_funds_it_and_that_it_moves_real_money() -> None:
    """The two facts an operator must not be able to miss, in the place they are
    most likely to look: `--help`.

    Asserted on the rendered help rather than on the source string, because what
    matters is that argparse actually shows it — a description the formatter
    swallowed would still be in the file.
    """
    help_text = uphold_dispute.build_parser().format_help()
    assert "THIS MOVES REAL FUNDS" in help_text
    assert "FUNDED BY THE PLATFORM, not clawed back from the agent" in help_text
    assert "--dry-run" in help_text
    assert "Signs nothing" in help_text


def test_help_tells_the_two_post_signature_rules_apart() -> None:
    """After a signature the right next move depends on which half failed, and
    the two rules point in opposite directions: a timed-out CREDIT is never
    re-run, a rating that did not land after a paid credit is re-run safely.
    Both sit in `--help`, beside their codes, so an operator who learned one
    cannot apply it to the other without reading the line that says otherwise."""
    help_text = uphold_dispute.build_parser().format_help()
    assert "10  the credit TIMED OUT and may still land — NEVER re-run" in help_text
    assert "12  the buyer IS paid but the rating did not land — re-running is SAFE" in help_text
    assert "retries the rating only, never the refund" in help_text
    assert "13  the ledger answered the rating with Replay" in help_text


def test_help_lists_the_in_flight_code_beside_the_timeout_it_behaves_like() -> None:
    """6 is a transfer on the network whose outcome nobody knows — the same
    situation as 10, and the block it prints opens with DO NOT RE-RUN. It used
    to fall under "every other non-zero code is a refusal before anything was
    signed", which is the one sentence that would make a wrapper author retry
    it. So it is in the table, with 10's instruction, and the sweeping sentence
    now names the codes it actually covers."""
    help_text = uphold_dispute.build_parser().format_help()
    assert "6  a credit for this dispute is IN FLIGHT and its outcome is unknown" in help_text
    assert "NEVER\n      re-run; reconcile it against the chain, exactly as 10 asks" in help_text
    assert "3, 4, 5, 7 and 8 are refusals raised before anything was signed." in help_text
    assert "every other non-zero code" not in help_text


def test_the_module_docstring_says_it_too() -> None:
    """`--help` is for the operator; the docstring is for whoever reads the tool
    before trusting its output. Both have to carry the funding disclosure —
    ADR 0002's trust model is only disclosed if it is disclosed everywhere."""
    doc = uphold_dispute.__doc__ or ""
    assert "moves real funds" in doc
    assert "funded by the PLATFORM, not clawed back from the agent" in doc


# ── the refusals: every one exits non-zero, before anything is signed ──────


def test_an_unknown_dispute_is_refused_and_names_the_likeliest_cause(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """The commonest way to see "no such dispute" is a DATABASE_URL pointing at
    a different store — an unset one is an in-memory store that knows nothing —
    so the refusal says that rather than leaving the operator to re-read the id."""
    forbid_uphold(monkeypatch)

    code, out = invoke(capsys, "--dispute-id", "dsp_nothere")

    assert code == uphold_dispute.EXIT_UNKNOWN_DISPUTE
    assert "unknown_dispute" in out
    assert "DATABASE_URL" in out


def test_a_dispute_whose_settlement_is_missing_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """Without the settlement there is no ceiling on the credit, and D4's whole
    point is that the credit is bounded by what actually settled. No record, no
    payout."""
    forbid_uphold(monkeypatch)
    seed(with_settlement=False)

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_NOTHING_TO_CREDIT
    assert "no settlement record" in out


def test_a_credited_dispute_is_previewed_as_a_rating_only_run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    credit: CreditSeam,
    chain_calls: list[str],
) -> None:
    """Since 4.04 a credited dispute is not a dead end: `uphold` re-attempts its
    rating and nothing else (D3), so this is how a rating that did not land is
    retried. The operator asking is still usually asking "did this one go
    through?", so the preview answers with the refund's hash and link, says in
    so many words that it will not be paid again, and says where the rating
    stands — and, being a dry run, touches neither the uphold nor the chain."""
    forbid_uphold(monkeypatch)
    seed(status="credited", refund_tx=REFUND_TX)

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    assert code == uphold_dispute.EXIT_OK
    assert "ALREADY CREDITED — the credit will NOT be paid again" in out
    assert f"https://stellar.expert/explorer/testnet/tx/{REFUND_TX}" in out
    assert "rating tx:  none on record" in out
    assert rating_id_hex() in out
    assert "exactly the rating above. No transfer." in out
    # No credit is planned for a dispute that has already been paid.
    assert "credit:    " not in out
    assert chain_calls == []


def test_a_credited_disputes_recorded_rating_is_never_previewed_as_landed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """A `rating_tx` on the record is written for a rating that landed and for
    one that timed out in flight alike, and a dry run asks the ledger nothing —
    so the preview calls it recorded and unconfirmed, and leaves the verdict to
    the live run that does ask."""
    forbid_uphold(monkeypatch)
    seed(status="credited", refund_tx=REFUND_TX, rating_tx=RATING_TX)

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    assert code == uphold_dispute.EXIT_OK
    assert f"rating tx:  {RATING_TX}" in out
    assert "on record, NOT confirmed — landed, or timed out in flight;" in out
    assert "RATED" not in out


def test_a_credited_dispute_with_no_refund_hash_is_still_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """`credited` with nothing a reviewer could open is a record to reconcile,
    not one to write a rating against — the rating is the consequence of a
    credit, and this one's credit is not evidenced."""
    forbid_uphold(monkeypatch)
    seed(status="credited")

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_NOT_ADJUDICABLE
    assert "already_credited" in out
    assert "nothing was signed" in out


def test_a_rejected_dispute_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """Rejection is an adjudication outcome and it is final here — this tool
    pays upheld disputes, it does not re-open them."""
    forbid_uphold(monkeypatch)
    seed(status="rejected")

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_NOT_ADJUDICABLE
    assert "already_rejected" in out


def test_a_dispute_already_in_crediting_is_refused_loudly(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """`crediting` means a claim is held and a transfer may be on the network.
    Re-running is exactly the wrong move, so the refusal says so in those words
    and points at what to check."""
    forbid_uphold(monkeypatch)
    seed(status="crediting", refund_tx=REFUND_TX)

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_IN_FLIGHT
    assert "DO NOT RE-RUN" in out
    assert "ALREADY IN FLIGHT" in out
    assert f"https://stellar.expert/explorer/testnet/tx/{REFUND_TX}" in out


def test_a_credit_over_the_cap_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """D5 is a refusal, never a clamp — paying the ceiling would hide the
    mistaken uphold the ceiling exists to catch."""
    forbid_uphold(monkeypatch)
    credit.refuses("refund_above_cap", "2.0000000 USDC exceeds MAX_REFUND_USDC=1.0000000")
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_ABOVE_CAP
    assert "refund_above_cap" in out
    assert "MAX_REFUND_USDC" in out


def test_nothing_to_credit_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """A step that never delivered was never part of what the buyer paid for."""
    forbid_uphold(monkeypatch)
    credit.refuses("nothing_to_credit", "step 1 (agt_seo) never delivered, so it was never paid for")
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_NOTHING_TO_CREDIT
    assert "nothing_to_credit" in out


def test_a_refusal_code_this_script_does_not_know_exits_unexpected(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """A refusal the refund service grows later must not be mistaken for one
    this script already understands — it gets the catch-all code, not the
    nearest neighbour."""
    forbid_uphold(monkeypatch)
    credit.refuses("some_future_refusal", "not a code this script maps")
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_UNEXPECTED
    assert "some_future_refusal" in out


def test_a_live_run_without_a_signing_configuration_is_refused_before_it_upholds(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """Refused BEFORE the uphold, so a missing environment variable cannot leave
    a dispute adjudicated by a run that then could not pay it. The refusal names
    what is missing and never a value."""
    forbid_uphold(monkeypatch)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)
    monkeypatch.setattr(settings, "stellar_asset_sac", "")
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "")
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_NOT_CONFIGURED
    assert "DISPUTE_REFUNDS_ENABLED=true" in out
    assert "STELLAR_SIGNING_KEY" in out
    assert "STELLAR_ASSET_SAC" in out
    # Without the ledger the credit would land and its rating could not.
    assert "STELLAR_REPUTATION_LEDGER" in out
    # The boot rule as `config._money_capable_config_requires_api_key` actually
    # applies it: the switch ALONE. Stating a signing key and a SAC as further
    # preconditions tells a deployer that a switch-on/signer-unwired deployment
    # will boot, and it hard-fails instead.
    assert "makes API_KEY mandatory on its own" in out
    assert "however little else on the refund path is wired up yet" in out


def test_nothing_this_script_prints_conjoins_the_boot_rule_with_a_signer(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """The validator fires on `dispute_refunds_enabled` alone, deliberately —
    conjoining it would leave a deployment that flips refunds on before wiring
    a signer booting with an empty API_KEY. So neither the module docstring nor
    any refusal may describe it as the switch plus a key plus a SAC."""
    forbid_uphold(monkeypatch)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)
    seed()

    _, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    doc = uphold_dispute.__doc__ or ""
    assert "makes\n`API_KEY` mandatory BY ITSELF" in doc
    for text in (doc, out):
        assert "a signing key and a SAC" not in text
        assert "signing key and asset SAC" not in text


def test_a_passed_config_check_says_what_it_did_not_check(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """`config_gap` is presence-only, and the gap it cannot see is the one that
    actually bites: a settler that is not the ledger's registered Scorer signs
    the credit perfectly well and has every rating reverted. Passing the check
    therefore says so, and names the probe that CAN answer it, rather than
    letting "configured" be heard as "the rating will be accepted"."""
    uphold.fails()
    seed()

    _, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert "Presence only." in out
    assert "not the ledger's registered Scorer" in out
    assert "ratings.writer = not_scorer" in out


def test_a_deployment_that_could_not_rate_is_refused_before_it_pays(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    credit: CreditSeam,
    configured: dict[str, str],
) -> None:
    """Everything a CREDIT needs is set, but ratings are switched off — so the
    service would pay and then decline to rate (`rating_writer.config_gap`),
    leaving a paid dispute that is not resolved. Refused while nothing has been
    signed, naming the setting in the service's own words."""
    forbid_uphold(monkeypatch)
    monkeypatch.setattr(settings, "reputation_enabled", False)
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_NOT_CONFIGURED
    assert "REPUTATION_ENABLED is false — so the dispute rating could not be written" in out
    assert "nothing was signed" in out


def test_every_refusal_code_is_non_zero_and_distinct() -> None:
    """The table an operator greps. Zero would read as success and a duplicate
    would make two different refusals indistinguishable to a wrapper script."""
    codes = {
        name: value for name, value in vars(uphold_dispute).items() if name.startswith("EXIT_") and name != "EXIT_OK"
    }
    assert codes
    assert all(value > 0 for value in codes.values())
    assert len(set(codes.values())) == len(codes)
    # 1 and 2 stay clear of the table: 2 is argparse's usage error and 1 is what
    # an unhandled traceback exits with.
    assert not {1, 2} & set(codes.values())


# ── the dry run: the default-safe path, and it must sign nothing ───────────


def _binding_lines(out: str) -> list[str]:
    """The bound lines the preview marked as the one that decides the credit."""
    return [line for line in out.splitlines() if "BINDS" in line]


def test_a_dry_run_signs_nothing_and_needs_no_signing_configuration(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    credit: CreditSeam,
    chain_calls: list[str],
    standing: StandingSeam,
) -> None:
    """The run an operator is told to do first, so it has to work on a machine
    that holds no key at all — and it must not reach the chain or the uphold on
    the way. Both are enforced: `forbid_uphold` here, and the stellar client
    booby-trap that every test in this file runs under, whose record of calls
    has to come back empty — not even a read, since the rating preview is
    computed, never simulated. The reputation read is held to the same: it is
    a live run's before-and-after, and a dry run makes neither."""
    forbid_uphold(monkeypatch)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)
    monkeypatch.setattr(settings, "stellar_signing_key", "")
    monkeypatch.setattr(settings, "stellar_asset_sac", "")
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    assert code == uphold_dispute.EXIT_OK
    assert "DRY RUN — nothing was signed and nothing moved." in out
    assert chain_calls == []
    assert standing.reads == []


def test_a_dry_run_prints_the_three_bounds_the_cap_and_the_payer(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """Everything an operator has to approve before real money moves, on one
    screen: which dispute, which settled step, each of D4's three bounds, D5's
    ceiling, the amount and the account it lands in."""
    forbid_uphold(monkeypatch)
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    assert code == uphold_dispute.EXIT_OK
    assert DISPUTE_ID in out and JOB in out and TASK in out
    assert "promised to the buyer when the dispute was opened" in out
    assert "DISPUTE_CREDITED_FRACTION" in out
    assert "ever settled on-chain for the whole workflow" in out
    assert "MAX_REFUND_USDC" in out
    assert PAYER in out
    assert f"https://stellar.expert/explorer/testnet/account/{PAYER}" in out
    assert f"{CREDITABLE_USDC:.7f} USDC  ->  {PAYER}" in out
    assert f"not clawed back from {AGENT}" in out


def test_the_preview_marks_the_bound_that_actually_binds(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """A clamped credit means two records disagree about money, and WHICH record
    held the number down is the thing an operator has to see. Here the workflow
    settled for less than the buyer was promised, so the settled total binds."""
    forbid_uphold(monkeypatch)
    credit.pays(0.02)
    seed(creditable_usdc=0.07, settled_usdc=0.02)

    _, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    assert _binding_lines(out) == ["    0.0200000 USDC  ever settled on-chain for the whole workflow   <- BINDS"]


def test_the_promise_binds_when_it_is_the_smallest_bound(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """The other direction: what the buyer was shown at opening time is a
    ceiling too, frozen then so a later policy change cannot raise it."""
    forbid_uphold(monkeypatch)
    credit.pays(0.01)
    seed(creditable_usdc=0.01)

    _, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    assert _binding_lines(out) == ["    0.0100000 USDC  promised to the buyer when the dispute was opened   <- BINDS"]


def test_equal_bounds_are_marked_once_and_the_agreement_is_counted(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """The ordinary case: a full-fraction credit on a workflow whose settled
    total is the disputed step's own price makes all three bounds the same
    number. Nothing is clamping the credit, so exactly one line carries the
    marker — the preview promises "which one binds", singular — and the two
    that agree with it are counted rather than left looking skipped."""
    forbid_uphold(monkeypatch)
    credit.pays(0.07)
    seed(creditable_usdc=0.07, settled_usdc=0.07)

    _, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    assert _binding_lines(out) == [
        "    0.0700000 USDC  promised to the buyer when the dispute was opened   <- BINDS"
        " (2 other bounds at the same figure)"
    ]


# ── the dry run shows the rating too: 4.04's half of the evidence ──────────


def rating_id_hex(step_index: int = STEP_INDEX) -> str:
    """The derived id the disputed step's rating is filed under.

    From the frozen derivation itself, so these tests check that the script
    prints the id the rating lane writes — not a second copy of the formula
    that could agree with this file and nothing else.
    """
    return dispute_rating.dispute_job_id(bytes.fromhex(JOB), step_index).hex()


def test_a_dry_run_shows_the_rating_it_would_write(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """Every value the ReputationLedger will be handed, approved before the key
    is touched: the agent, the rating, its kind, and the weight in both units —
    stroops because that is what goes on-chain, USDC because that is what an
    operator can check against the step's price (D2: the quoted price, as every
    rating is weighted)."""
    forbid_uphold(monkeypatch)
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    weight = reputation_svc.rating_weight_stroops(STEPS[STEP_INDEX].price_usdc)
    assert code == uphold_dispute.EXIT_OK
    assert weight == 700_000
    assert f"agent:     {AGENT}" in out
    assert f"rating:    {dispute_rating.DISPUTE_RATING} / 100" in out
    assert 'kind = "dispute"' in out
    assert f"weight:    {weight} stroops = 0.0700000 USDC" in out
    assert "then write exactly the rating above" in out


def test_the_preview_stacks_the_two_ids_and_underlines_the_bytes_they_share(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """The shared prefix is the whole reason the derivation was chosen: it is
    how a grant reviewer ties the rating to the job on Stellar Expert without
    reading our code. So the layout is pinned, not just the presence of the ids
    — both printed whole in the same column, the first 8 bytes (16 hex
    characters) underlined beneath, and a one-line reason beside the underline.
    """
    forbid_uphold(monkeypatch)
    seed()

    _, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    derived = rating_id_hex()
    lines = out.splitlines()
    job_line = next(line for line in lines if line.lstrip().startswith("job id:"))
    rating_line = next(line for line in lines if line.lstrip().startswith("rating id:"))
    underline = lines[lines.index(rating_line) + 1]
    column = job_line.index(JOB)

    assert derived[:16] == JOB[:16] and derived != JOB
    assert rating_line.index(derived) == column
    assert underline[:column].strip() == ""
    assert underline[column : column + 17] == "^" * 16 + " "
    assert "Stellar Expert" in underline[column + 17 :]


def test_a_dispute_that_could_never_be_rated_is_refused_before_it_is_paid(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """A step missing from the settlement has no price to weight a rating by.
    Paying the credit anyway would leave a dispute that can never be fully
    resolved, so the run stops while nothing is signed — the credit waits,
    which is recoverable, where a half-resolved dispute is not."""
    forbid_uphold(monkeypatch)
    seed(step_index=5)

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_UNEXPECTED
    assert "rating_not_derivable" in out
    assert "nothing was signed" in out


# ── the live run: the verdict comes off the STORE, never off the call ──────


class UpholdSeam:
    """What `dispute_svc.uphold` does to the store, for one test.

    Each method writes the state the real service leaves behind for one of the
    three answers a submission can give (D3's taxonomy), so what is exercised is
    the script reading that state back — which is the whole of how it decides
    whether money moved.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.calls: list[str] = []

    def _bind(self, effect: Any) -> None:
        # The real signature, keyword-only observer included, and not a
        # catch-all: a stand-in that swallowed any argument would keep passing
        # after the script and the service had drifted apart. These seams write
        # refund states only, so no rating is ever submitted and the observer —
        # like the real one on these paths — is never told anything.
        async def _uphold(dispute_id: str, *, on_rating: dispute_svc.RatingObserver | None = None) -> Any:
            self.calls.append(dispute_id)
            await effect(dispute_id)
            return await dispute_store.get_dispute_store().get_dispute(dispute_id)

        self._monkeypatch.setattr(dispute_svc, "uphold", _uphold, raising=False)

    def _writes(self, status: DisputeStatus, refund_tx: str | None) -> Any:
        async def _effect(dispute_id: str) -> None:
            await dispute_store.get_dispute_store().append_status(dispute_id, status, refund_tx=refund_tx)

        return _effect

    def lands_without_a_hash(self) -> None:
        """`credited` with nothing a reviewer could open — a state the store
        permits and the script must not report as evidence."""
        self._bind(self._writes("credited", None))

    def times_out(self, tx: str | None = REFUND_TX) -> None:
        """TIMEOUT — the claim stays held, the dispute stays `crediting` with the
        in-flight hash recorded, and `uphold` refuses with `refund_unconfirmed`
        rather than returning (D3). The write happens before the refusal, which
        is exactly why the verdict is read off the record."""
        self.raises(
            dispute_svc.DisputeError(
                "refund_unconfirmed",
                "the credit was submitted and its outcome is unknown",
                504,
            ),
            leaves=("crediting", tx),
        )

    def fails(self) -> None:
        """FAILED — nothing moved, so the claim was released, the dispute is back
        at `upheld`, and `uphold` refuses with `refund_failed`."""
        self.raises(
            dispute_svc.DisputeError(
                "refund_failed",
                "the credit transfer did not settle, so nothing was paid",
                502,
            ),
            leaves=("upheld", None),
        )

    def raises(self, exc: BaseException, leaves: tuple[DisputeStatus, str | None] | None = None) -> None:
        """Blow up, optionally after leaving the store in `leaves`."""

        async def _effect(dispute_id: str) -> None:
            if leaves is not None:
                await self._writes(*leaves)(dispute_id)
            raise exc

        self._bind(_effect)


@pytest.fixture
def uphold(monkeypatch: pytest.MonkeyPatch) -> UpholdSeam:
    return UpholdSeam(monkeypatch)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A process that could sign: the switch on, a settler key and a SAC set.

    The values are fictional and never reach the chain — the stellar client is
    booby-trapped for every test in this file. They exist so the live path gets
    past `check_config`, and so the redaction tests have concrete secrets that
    must not appear anywhere in the output.

    `DATABASE_URL` is deliberately NOT among them: setting it would make
    `get_dispute_store()` resolve a Postgres store and dial a database from a
    hermetic suite.
    """
    secrets = {
        "stellar_signing_key": "SB" + "K7QX4M2" * 7,
        "api_key": "adjudicator-key-" + "9f3c" * 6,
    }
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    monkeypatch.setattr(settings, "stellar_asset_sac", "CSAC" + "7Z2Q" * 12)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    for name, value in secrets.items():
        monkeypatch.setattr(settings, name, value)
    return secrets


class RatingSeam:
    """What the ReputationLedger answers the dispute rating with, for one test.

    Bound at `dispute_rating.submit_dispute_rating` — the rating lane's own
    boundary, whose answer is the frozen `RatingOutcome` — so everything above
    it is the real adjudication code deciding what to record, and the script
    reading that record back. `calls` names each dispute rated: a rating-only
    re-run must reach this and nothing that pays.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.calls: list[str] = []
        self.answers("SUCCESS", RATING_TX)

    def answers(self, status: dispute_rating.RatingStatus, tx_hash: str | None = None) -> None:
        async def _submit(dispute: DisputeRecord, settlement: SettlementRecord) -> dispute_rating.RatingOutcome:
            self.calls.append(dispute.id)
            step = settlement.step(dispute.step_index)
            assert step is not None
            return dispute_rating.RatingOutcome(
                status,
                tx_hash,
                rating_id_hex(dispute.step_index),
                dispute_rating.DISPUTE_RATING,
                reputation_svc.rating_weight_stroops(step.price_usdc),
            )

        self._monkeypatch.setattr(dispute_rating, "submit_dispute_rating", _submit)

    def raises(self, exc: BaseException) -> None:
        """The submit raises instead of answering — which the service, holding a
        paid dispute, absorbs and logs rather than letting out."""

        async def _submit(dispute: DisputeRecord, settlement: SettlementRecord) -> dispute_rating.RatingOutcome:
            self.calls.append(dispute.id)
            raise exc

        self._monkeypatch.setattr(dispute_rating, "submit_dispute_rating", _submit)


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> RatingSeam:
    """The dispute rating's ledger, landing the rating by default."""
    return RatingSeam(monkeypatch)


@pytest.fixture
def paying(
    monkeypatch: pytest.MonkeyPatch, credit: CreditSeam, configured: dict[str, str], ledger: RatingSeam
) -> list[str]:
    """The REAL `dispute_svc.uphold`, with only the chain replaced.

    The transfer lands (`refund_svc.credit_refund`) and the ledger answers as
    `ledger` says; every rule between them — the claim, what `credited`
    records, when the rating fires, what a REPLAY means for this dispute — is
    the adjudication lane's shipped code. That is the point: the script's
    verdict is read off a record that service wrote, so the test has to let
    the service write it. The list names each dispute a transfer was signed
    for, so a rating-only re-run can be held to signing none.
    """
    transfers: list[str] = []

    async def _credit(dispute: DisputeRecord, amount_usdc: float) -> refund_svc.RefundOutcome:
        transfers.append(dispute.id)
        return refund_svc.RefundOutcome("SUCCESS", REFUND_TX, amount_usdc)

    monkeypatch.setattr(refund_svc, "credit_refund", _credit)
    return transfers


def stored() -> DisputeRecord:
    """The dispute as the store holds it after a run."""
    record = asyncio.run(dispute_store.get_dispute_store().get_dispute(DISPUTE_ID))
    assert record is not None
    return record


def test_a_clean_live_run_prints_both_transactions_as_the_dispute_evidence(
    capsys: pytest.CaptureFixture[str], paying: list[str], ledger: RatingSeam
) -> None:
    """The acceptance criterion of both stories, in the one form a grant
    reviewer can check: the credit's hash and full Stellar Expert URL with the
    funding disclosure beside it (4.03), the rating's hash and URL with the
    derived id it is filed under (4.04), and then the two links again, side by
    side, stated as the dispute's on-chain evidence — so neither is ever quoted
    without the other."""
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    refund_url = f"https://stellar.expert/explorer/testnet/tx/{REFUND_TX}"
    rating_url = f"https://stellar.expert/explorer/testnet/tx/{RATING_TX}"
    assert code == uphold_dispute.EXIT_OK
    assert paying == [DISPUTE_ID] and ledger.calls == [DISPUTE_ID]
    assert f"CREDITED — {CREDITABLE_USDC:.7f} USDC paid to {PAYER}" in out
    assert "status:    credited" in out
    assert f"tx:        {REFUND_TX}" in out
    assert "The disputed agent was NOT charged" in out
    assert f'RATED — {AGENT} rated {dispute_rating.DISPUTE_RATING}/100, kind "dispute", by this run' in out
    assert f"rating tx: {RATING_TX}" in out
    assert f"rating id: {rating_id_hex()}" in out

    evidence = out[out.index("ON-CHAIN EVIDENCE") :]
    assert f"dispute {DISPUTE_ID}: both transactions, together" in evidence
    assert f"refund: {refund_url}" in evidence
    assert f"rating: {rating_url}" in evidence
    assert stored().rating_tx == RATING_TX


def test_a_timed_out_transfer_is_reported_as_maybe_landed_and_never_as_a_failure(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """The case most likely to be mishandled at 2am, and the only one where
    getting it wrong pays the buyer twice.

    So the output has to carry all four things: that it may still land, that the
    script must not be re-run, that the claim is still held on purpose, and what
    to check on-chain before touching anything.
    """
    uphold.times_out()
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_TIMEOUT
    assert "TIMED OUT" in out and "MAY STILL LAND" in out
    assert "DO NOT RE-RUN THIS SCRIPT YET — A TRANSFER MAY BE LIVE" in out
    assert "STILL HELD" in out
    assert f"https://stellar.expert/explorer/testnet/tx/{REFUND_TX}" in out
    assert f"https://stellar.expert/explorer/testnet/account/{PAYER}" in out
    assert "release_refund_claim" in out
    assert "FAILED" not in out.split("TIMED OUT")[0]
    assert "refund_unconfirmed" in out
    # The one sentence that must never appear here: a submission that timed out
    # WAS signed, and telling an operator otherwise is how it gets retried.
    assert "nothing was signed" not in out


def test_the_timeout_block_asks_for_the_one_re_run_that_finishes_the_dispute(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """The reconciled dispute is not finished when its credit is recorded.

    An operator who reads the chain and writes `credited` by hand leaves a
    dispute with a refund hash and NO rating — nothing in that write can
    produce one. Exactly one re-run does, and it signs nothing: `uphold`
    refuses to transfer for a `credited` dispute. The block used to forbid it
    on the false premise that it would pay the buyer twice, so the premise is
    pinned out as well as the instruction pinned in.
    """
    uphold.times_out()
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_TIMEOUT
    assert "credited_usdc=<the amount the transfer moved>" in out
    assert "THEN re-run this script once." in out
    assert "signs no second transfer for a `credited` dispute" in out
    # The false sentence this block used to carry, and the rule it wrongly
    # blocked: a bare re-run is refused, never a second payment.
    assert "would credit them a second time" not in out
    assert "refused (exit 6) rather than dangerous" in out


def test_a_credit_claimed_by_another_caller_mid_run_is_not_called_a_timeout(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """The narrow race: the dispute was payable when this run read it, and
    another caller claimed it before the uphold landed. `uphold` refuses with
    `refund_in_flight` and the dispute reads `crediting` — the same record a
    timeout leaves, and the same instruction follows. But it is not this run's
    transfer, so the headline says whose it is and the code is 6, not the 10
    that means "this process signed and lost the answer"."""
    uphold.raises(
        dispute_svc.DisputeError("refund_in_flight", "a credit for this dispute is already in flight", 409),
        leaves=("crediting", REFUND_TX),
    )
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_IN_FLIGHT
    assert "ALREADY IN FLIGHT" in out
    assert "TIMED OUT" not in out
    assert "DO NOT RE-RUN THIS SCRIPT YET — A TRANSFER MAY BE LIVE" in out
    assert f"https://stellar.expert/explorer/testnet/tx/{REFUND_TX}" in out
    assert "nothing was signed" not in out


def test_a_timeout_with_no_hash_still_refuses_to_call_it_a_failure(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """A submission can time out before the client has a hash to report. The
    transfer may still have landed, so the answer is the payer's account rather
    than a shrug — and certainly not a retry."""
    uphold.times_out(tx=None)
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_TIMEOUT
    assert "none recorded — the submission returned no hash at all." in out
    assert f"look for a credit of about {CREDITABLE_USDC:.7f} USDC" in out


def test_a_definitively_failed_transfer_is_payable_again(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """`upheld` after a live run means the claim was released because nothing
    moved — the one outcome where running this again is the right move, so it
    says so and exits on its own code."""
    uphold.fails()
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_TRANSFER_FAILED
    assert "the transfer FAILED" in out
    assert "running this again once the cause is fixed" in out


def test_an_unexpected_exception_does_not_override_what_the_store_says(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """The reason the verdict is read back off the record rather than taken from
    the call: an exception says nothing about whether the transfer was
    submitted. Here one is raised after the claim was taken, and the dispute is
    still `crediting` — which is a timeout, not a crash, however it surfaced."""
    uphold.raises(RuntimeError("connection reset while polling"), leaves=("crediting", REFUND_TX))
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_TIMEOUT
    assert "RuntimeError: connection reset while polling" in out
    assert "DO NOT RE-RUN THIS SCRIPT YET — A TRANSFER MAY BE LIVE" in out


def test_a_service_refusal_leaves_the_dispute_untouched_and_says_so(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """A rule refusal out of the dispute service is raised before anything is
    written, and the report says that in as many words rather than listing a
    status the operator then has to interpret."""
    uphold.raises(dispute_svc.DisputeError("dispute_window_closed", "the dispute window closed", 409))
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_NOT_ADJUDICABLE
    assert "dispute_window_closed" in out
    assert "untouched at `open`" in out


def test_a_credited_dispute_with_no_hash_is_not_treated_as_evidence(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """`credited` without a transaction is not something to print a PASS for:
    there is nothing a reviewer could open. It exits on the catch-all and asks
    for an on-chain reconciliation."""
    uphold.lands_without_a_hash()
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_UNEXPECTED
    assert "reconcile the payer's account on-chain" in out


# ── the rating half of a live run (4.04): it lands, or it says what it is ──


def test_a_rating_that_failed_after_the_credit_landed_says_the_buyer_is_paid_and_rerunning_is_safe(
    capsys: pytest.CaptureFixture[str], paying: list[str], ledger: RatingSeam
) -> None:
    """The outcome an operator who learned 4.03's "never re-run" will get
    wrong. The buyer HAS been paid and the rating did not land — and here a
    re-run is RIGHT, because it retries the rating and never the refund. Both
    halves are said in as many words, with the reason the rule is reversed,
    on an exit code of its own."""
    ledger.answers("FAILED")
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_RATING_NOT_LANDED
    assert "THE BUYER HAS BEEN PAID — BUT THE AGENT'S DISPUTE RATING DID NOT LAND." in out
    assert "RE-RUNNING THIS SCRIPT IS SAFE HERE: IT RETRIES THE RATING, NEVER THE REFUND." in out
    assert "Why re-running is right here, when a timed-out CREDIT must never be re-run" in out
    assert "FAILED — the ledger refused it; nothing was written." in out
    assert f"python scripts/uphold_dispute.py --dispute-id {DISPUTE_ID}" in out
    # Never the 4.03 advice, never a claim that nothing was signed, never a PASS.
    assert "DO NOT RE-RUN" not in out
    assert "nothing was signed" not in out
    assert "RATED" not in out and "ON-CHAIN EVIDENCE" not in out
    record = stored()
    assert record.status == "credited" and record.refund_tx == REFUND_TX and record.rating_tx is None


def test_a_rating_that_timed_out_is_not_reported_as_landed_though_its_hash_is_on_record(
    capsys: pytest.CaptureFixture[str], paying: list[str], ledger: RatingSeam
) -> None:
    """The case the record alone cannot settle: the service records a timed-out
    rating's in-flight hash as `rating_tx`, exactly as it records a landed one.
    The script does not call it landed on the strength of that — the ledger
    never confirmed it — so it exits on 12 with the hash to check, and says a
    re-run is safe: if it lands late, the re-run is answered with Replay."""
    in_flight = "ab" * 32
    ledger.answers("TIMEOUT", in_flight)
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert stored().rating_tx == in_flight
    assert code == uphold_dispute.EXIT_RATING_NOT_LANDED
    assert "TIMED OUT — submitted and unconfirmed; it may still land." in out
    assert f"check it:  https://stellar.expert/explorer/testnet/tx/{in_flight}" in out
    assert "RE-RUNNING THIS SCRIPT IS SAFE HERE" in out
    assert "RATED" not in out and "ON-CHAIN EVIDENCE" not in out


def test_a_rating_success_with_no_hash_is_not_reported_as_landed(
    capsys: pytest.CaptureFixture[str], paying: list[str], ledger: RatingSeam
) -> None:
    """A SUCCESS that carries no hash leaves nothing a reviewer could open, so
    it is the same unknown as a timeout — never a PASS, and never evidence."""
    ledger.answers("SUCCESS", None)
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_RATING_NOT_LANDED
    assert "submitted and unconfirmed" in out
    assert "RATED" not in out and "ON-CHAIN EVIDENCE" not in out


def test_a_rating_collision_is_judged_from_the_ledgers_answer_and_says_the_consequence_did_not_land(
    capsys: pytest.CaptureFixture[str], paying: list[str], ledger: RatingSeam
) -> None:
    """A Replay for a dispute that records no attempt of its own (D4): the
    ledger holds a rating under this dispute's id that this dispute never
    wrote. Judged exactly as the service judges it — on the answer the ledger
    gave THIS run and the record it had — and answered on its own exit code,
    naming the agent, the dispute and the derived id, and saying outright that
    the reputation consequence did NOT land and the dispute is not resolved."""
    ledger.answers("REPLAY")
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_RATING_COLLISION
    assert "RATING COLLISION — THE AGENT'S REPUTATION CONSEQUENCE DID NOT LAND." in out
    assert "The ledger answered this run's rating with Replay" in out
    assert f"agent:     {AGENT}" in out
    assert f"dispute:   {DISPUTE_ID}" in out
    assert f"rating id: {rating_id_hex()}" in out
    assert f"https://stellar.expert/explorer/testnet/contract/{LEDGER}" in out
    assert "must not" in out and "be reported as resolved" in out
    assert "RATED" not in out and "ON-CHAIN EVIDENCE" not in out
    assert stored().rating_tx is None


def test_an_empty_rating_record_is_never_diagnosed_as_a_collision(
    capsys: pytest.CaptureFixture[str], paying: list[str], ledger: RatingSeam
) -> None:
    """The other side of the same rule. A FAILED rating leaves exactly the
    record a collision does — `credited`, no `rating_tx` — so a script reading
    the record alone could not tell them apart and must not try. With the
    ledger's answer a failure, it is reported as a failure, on 12."""
    ledger.answers("FAILED")
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert stored().rating_tx is None
    assert code == uphold_dispute.EXIT_RATING_NOT_LANDED
    assert "COLLISION" not in out


def test_a_replay_of_a_recorded_rating_confirms_it_landed(
    capsys: pytest.CaptureFixture[str], paying: list[str], ledger: RatingSeam
) -> None:
    """The other half of D4: a Replay for a dispute that DOES record an
    attempt is that attempt, landed — the ledger refused a second copy. Here a
    rating that timed out on an earlier run is re-confirmed by a rating-only
    re-run, which signs no transfer, and the run ends clean with both links."""
    seed(status="credited", refund_tx=REFUND_TX, rating_tx=RATING_TX)
    ledger.answers("REPLAY")

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_OK
    assert paying == []
    assert "by an earlier run;" in out
    assert "refused this run's copy as a replay" in out
    assert f"rating: https://stellar.expert/explorer/testnet/tx/{RATING_TX}" in out
    assert f"refund: https://stellar.expert/explorer/testnet/tx/{REFUND_TX}" in out


def test_a_rerun_after_a_rating_that_did_not_land_writes_the_rating_and_signs_no_transfer(
    capsys: pytest.CaptureFixture[str], paying: list[str], ledger: RatingSeam
) -> None:
    """What exit 12 promises, kept: the dispute is `credited` with no rating,
    the operator re-runs, and the run goes past the refund to the rating
    alone — no transfer is signed — and ends with both transactions printed as
    the dispute's evidence. The credit is reported as the earlier run's, so
    the re-run can never be read as a second payment."""
    seed(status="credited", refund_tx=REFUND_TX)

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_OK
    assert paying == []
    assert ledger.calls == [DISPUTE_ID]
    assert "CREDITED EARLIER" in out and "this one signed no transfer" in out
    assert f"refund: https://stellar.expert/explorer/testnet/tx/{REFUND_TX}" in out
    assert f"rating: https://stellar.expert/explorer/testnet/tx/{RATING_TX}" in out
    assert stored().rating_tx == RATING_TX


def test_a_rating_that_landed_but_was_never_recorded_asks_for_the_record_before_any_rerun(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, paying: list[str], ledger: RatingSeam
) -> None:
    """The ledger confirmed the rating and the store write recording it failed.
    Both transactions exist, but a re-run would now meet a Replay with no
    attempt on record and call this dispute's own rating a collision — so it
    exits on the catch-all with the one write that closes it."""
    seed()
    store = dispute_store.get_dispute_store()
    append = store.append_status

    async def _append(dispute_id: str, status: DisputeStatus, **kwargs: Any) -> DisputeRecord:
        if kwargs.get("rating_tx"):
            raise ConnectionError("the store went away")
        return await append(dispute_id, status, **kwargs)

    monkeypatch.setattr(store, "append_status", _append)

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_UNEXPECTED
    assert "THE RATING LANDED — BUT THE DISPUTE DOES NOT RECORD IT." in out
    # The ledger vouched for it, so the hint records it as confirmed — story 4.06's
    # receipt shows an unconfirmed rating as pending, never as the agent's consequence.
    assert f"append_status({DISPUTE_ID!r}, 'credited', rating_tx={RATING_TX!r}, rating_confirmed=True)" in out
    assert f"https://stellar.expert/explorer/testnet/tx/{RATING_TX}" in out
    assert "ON-CHAIN EVIDENCE" not in out


def test_the_script_never_rebinds_the_rating_service(paying: list[str], ledger: RatingSeam) -> None:
    """The ledger's answer reaches the script through `uphold`'s declared
    `on_rating` observer, so an operator tool has no reason to reach into the
    service and replace one of its functions — and must not: anything else in
    the process calling through a replaced attribute would be calling the
    tool's code. Whatever was bound before the run is bound after it."""
    before = dispute_rating.submit_dispute_rating
    seed()

    uphold_dispute.main(["--dispute-id", DISPUTE_ID])

    assert dispute_rating.submit_dispute_rating is before


# ── the agent's dispute rate, before and after: never a reason to fail ─────


def test_a_live_run_prints_how_the_agents_dispute_rate_moved(
    capsys: pytest.CaptureFixture[str], paying: list[str], standing: StandingSeam
) -> None:
    """The card's "the agent's dispute rate moves", on the same screen as the
    transaction that moved it: read before the uphold and after it, with the
    lifetime counters beside the rate, since each landed dispute rating adds
    exactly one to both."""
    standing.reads_as(rep(0, 5), rep(1, 6))
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_OK
    assert standing.reads == [AGENT, AGENT]
    assert "before this run:  0 bps   (0 of 5 ratings disputed)" in out
    assert "after this run:   1666 bps   (1 of 6 ratings disputed)" in out
    assert "moved:            +1666 bps" in out


@pytest.mark.parametrize(
    "unreadable",
    [RuntimeError("rpc down"), rep(0, 0, degraded=True)],
    ids=["raises", "degraded-prior"],
)
def test_a_reputation_read_that_fails_never_fails_the_run(
    unreadable: reputation_svc.RepInfo | BaseException,
    capsys: pytest.CaptureFixture[str],
    paying: list[str],
    standing: StandingSeam,
) -> None:
    """Both reads fail — one raising, one answering with the degraded prior an
    unreachable ledger produces — and the run still pays, rates and exits
    clean: the transactions are the evidence, the rate is a view of their
    effect. The prior is never printed as a rate, because a cold-start prior
    would give the agent a clean record the ledger may contradict."""
    standing.reads_as(unreadable, unreadable)
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_OK
    assert "before this run:  could not be read" in out
    assert "after this run:   could not be read" in out
    assert "A read never fails this run" in out
    assert "ON-CHAIN EVIDENCE" in out


# ── no line of output can carry a secret ───────────────────────────────────


@pytest.mark.parametrize(
    ("status", "tx_hash", "expected"),
    [
        ("SUCCESS", RATING_TX, uphold_dispute.EXIT_OK),
        ("FAILED", None, uphold_dispute.EXIT_RATING_NOT_LANDED),
        ("TIMEOUT", RATING_TX, uphold_dispute.EXIT_RATING_NOT_LANDED),
        ("REPLAY", None, uphold_dispute.EXIT_RATING_COLLISION),
    ],
)
def test_no_line_of_a_live_run_contains_the_signing_key_or_the_api_key(
    status: dispute_rating.RatingStatus,
    tx_hash: str | None,
    expected: int,
    capsys: pytest.CaptureFixture[str],
    paying: list[str],
    ledger: RatingSeam,
    configured: dict[str, str],
) -> None:
    """The whole of a run that pays and rates, checked against the two
    credentials this process holds — once for every answer the rating can get,
    since each prints its own block. Both streams: a key on stderr is as
    published as one on stdout once the terminal is screenshotted into an
    evidence bundle."""
    ledger.answers(status, tx_hash)
    seed()

    assert uphold_dispute.main(["--dispute-id", DISPUTE_ID]) == expected

    captured = capsys.readouterr()
    for value in configured.values():
        assert value not in captured.out
        assert value not in captured.err


def test_a_rating_submit_that_quotes_the_key_back_leaks_it_nowhere(
    capsys: pytest.CaptureFixture[str], paying: list[str], ledger: RatingSeam, configured: dict[str, str]
) -> None:
    """The rating's realistic leak: a signer failure whose message quotes the
    key. Once the buyer is paid the service swallows it — it must, or a paid
    refund would read as a failed one — and logs it with its traceback, so
    stderr is where the key would surface. That stream is watched through the
    very handler an operator's terminal gets (`redacted_handler`), and it has to
    carry the failure, so the check is not passing on an empty buffer."""
    key = configured["stellar_signing_key"]
    ledger.raises(RuntimeError(f"could not sign with {key}"))
    seed()
    stderr = io.StringIO()
    handler = uphold_dispute.redacted_handler(stderr)
    logging.getLogger().addHandler(handler)
    try:
        code = uphold_dispute.main(["--dispute-id", DISPUTE_ID])
    finally:
        logging.getLogger().removeHandler(handler)

    out = capsys.readouterr().out
    assert code == uphold_dispute.EXIT_RATING_NOT_LANDED
    assert "NO ANSWER" in out
    assert DISPUTE_ID in stderr.getvalue() and "[redacted]" in stderr.getvalue()
    assert key not in out
    assert key not in stderr.getvalue()


def test_a_secret_quoted_back_inside_an_error_is_masked(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """The realistic leak, and the reason the guarantee is made at the output
    path rather than line by line: an exception from a library quotes back what
    it was given, and the unexpected-exception branch prints exception text this
    script never wrote."""
    key = configured["stellar_signing_key"]
    uphold.raises(RuntimeError(f"submit rejected for {key}"), leaves=("crediting", REFUND_TX))
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_TIMEOUT
    assert key not in out
    assert "[redacted]" in out


def test_say_masks_a_secret_shaped_token_that_is_not_even_ours(capsys: pytest.CaptureFixture[str]) -> None:
    """`say` masks by shape as well as by value, so a key pasted into a message
    from somewhere else — another deployment's, a mnemonic in an error — is
    caught too. That is what makes the guarantee a property of the helper rather
    than of what this deployment happens to have configured."""
    # A StrKey secret seed is an S and exactly 55 more base32 characters —
    # the shape the redactor recognises, and the one a pasted key really has.
    foreign = "S" + "CDEFGH2345" * 5 + "ABCDE"

    uphold_dispute.say(f"submit rejected for {foreign}")

    assert foreign not in capsys.readouterr().out


def test_nothing_reaches_stdout_except_through_say(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    paying: list[str],
    standing: StandingSeam,
) -> None:
    """The structural half of the guarantee: with `say` diverted, the script
    prints nothing at all.

    A test that only checked the current output for secrets would pass for as
    long as nobody added a bare `print`. This one fails the moment somebody
    does, on the preview and on the report path alike — the rating's blocks and
    the dispute-rate lines included, since the live run here pays, rates and
    reads the ledger both sides — which is the difference between a promise
    about today's lines and one about tomorrow's.
    """
    printed: list[str] = []
    monkeypatch.setattr(uphold_dispute, "say", lambda line="": printed.append(line))

    seed()
    standing.reads_as(rep(0, 5), rep(1, 6))
    assert uphold_dispute.main(["--dispute-id", DISPUTE_ID, "--dry-run"]) == uphold_dispute.EXIT_OK
    assert uphold_dispute.main(["--dispute-id", DISPUTE_ID]) == uphold_dispute.EXIT_OK

    assert printed
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("refunds_disabled", uphold_dispute.EXIT_NOT_CONFIGURED),
        ("refunds_not_configured", uphold_dispute.EXIT_NOT_CONFIGURED),
        ("unknown_dispute", uphold_dispute.EXIT_UNKNOWN_DISPUTE),
        ("dispute_rejected", uphold_dispute.EXIT_NOT_ADJUDICABLE),
        ("settlement_missing", uphold_dispute.EXIT_NOTHING_TO_CREDIT),
        ("refund_above_cap", uphold_dispute.EXIT_ABOVE_CAP),
        ("nothing_to_credit", uphold_dispute.EXIT_NOTHING_TO_CREDIT),
    ],
)
def test_each_adjudication_refusal_carries_through_to_its_own_exit_code(
    code: str,
    expected: int,
    capsys: pytest.CaptureFixture[str],
    credit: CreditSeam,
    uphold: UpholdSeam,
    configured: dict[str, str],
) -> None:
    """`uphold` answers every refusal — its own and the two it re-raises out of
    the refund service — as a `DisputeError` carrying a stable code. Each one
    reaches a distinct exit, so a wrapper script can tell "the cap stopped it"
    from "there was nothing to credit" without reading prose.

    All of them are raised before anything is signed, so all of them keep the
    `nothing was signed` wording.
    """
    uphold.raises(dispute_svc.DisputeError(code, f"refused: {code}", 409))
    seed()

    exit_code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert exit_code == expected
    assert code in out
    assert "nothing was signed" in out
