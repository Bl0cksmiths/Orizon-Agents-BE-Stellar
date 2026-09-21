"""The operator tool that upholds a dispute and pays it (`scripts/uphold_dispute.py`).

Story 4.03's first acceptance criterion — a credit a grant reviewer can open on
Stellar Expert — is the one thing CI cannot produce, because it needs the funded
settler key and that key never reaches a CI runner. What CI *can* do is make the
tool safe to point at real money at 2am, and that is the whole of this file.
Four properties, in the order they matter:

  - the preview signs NOTHING — the stellar client is not reached at all;
  - every refusal exits non-zero, with its own code and a sentence saying why;
  - a timed-out transfer is reported as "may still land, do not re-run", never
    as a failure, because the opposite reading credits the buyer twice;
  - no line of output can carry a secret.

Hermetic: the in-memory dispute store, a stubbed refund service and a stubbed
uphold. Nothing here touches the chain, a database or the network — and the
stellar client is deliberately booby-trapped, so "it never signs" is checked
rather than asserted in a docstring.

`refund_svc.creditable_for` (with `RefundRefused`) and `dispute_svc.uphold_dispute`
land on sibling lanes of this same story. What is pinned here is what THIS
script does with each answer those two calls can give, which is what the script
owns and what a merge cannot change silently. The seams bind to the real types
when the modules already carry them and to stand-ins with the same surface when
they do not, so the file holds either side of that merge.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import app.stellar.client as sc
from app.config import settings
from app.services import dispute_store, dispute_svc, refund_svc
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
    """Fail the test if anything calls `dispute_svc.uphold_dispute`.

    Used on every path that must stop before signing. Asserting the refusal's
    exit code alone would pass just as happily on a script that refused loudly
    and paid anyway.
    """

    async def _never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("uphold_dispute was called on a path that must never sign")

    monkeypatch.setattr(dispute_svc, "uphold_dispute", _never, raising=False)


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
def _no_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Booby-trap the stellar client for every test in this file.

    Every entry point the refund path could reach is replaced with something
    that fails the test loudly. "The dry run signs nothing" then becomes a
    property the suite enforces rather than a claim: a line added later that
    derives the settler's public key, simulates, or submits is caught here
    instead of on testnet.
    """

    def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the stellar client was called — this path must never reach the chain")

    for name in ("invoke_with_server_key_async", "signer_public_key", "submit_rating_async", "contract_ids"):
        monkeypatch.setattr(sc, name, _forbidden)


def seed(
    *,
    status: DisputeStatus = "open",
    refund_tx: str | None = None,
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


def test_an_already_credited_dispute_is_refused_and_reprints_its_evidence(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """The refusal that matters most, because the operator asking for it is
    usually asking "did this one go through?" — so it answers with the hash and
    the explorer link instead of only saying no."""
    forbid_uphold(monkeypatch)
    seed(status="credited", refund_tx=REFUND_TX)

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_NOT_ADJUDICABLE
    assert "already_credited" in out
    assert REFUND_TX in out
    assert f"https://stellar.expert/explorer/testnet/tx/{REFUND_TX}" in out


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
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_NOT_CONFIGURED
    assert "DISPUTE_REFUNDS_ENABLED=true" in out
    assert "STELLAR_SIGNING_KEY" in out
    assert "STELLAR_ASSET_SAC" in out


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
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, credit: CreditSeam
) -> None:
    """The run an operator is told to do first, so it has to work on a machine
    that holds no key at all — and it must not reach the chain or the uphold on
    the way. Both are enforced: `forbid_uphold` here, and the stellar client
    booby-trap that every test in this file runs under."""
    forbid_uphold(monkeypatch)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)
    monkeypatch.setattr(settings, "stellar_signing_key", "")
    monkeypatch.setattr(settings, "stellar_asset_sac", "")
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID, "--dry-run")

    assert code == uphold_dispute.EXIT_OK
    assert "DRY RUN — nothing was signed and nothing moved." in out


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


# ── the live run: the verdict comes off the STORE, never off the call ──────


class UpholdSeam:
    """What `dispute_svc.uphold_dispute` does to the store, for one test.

    Each method writes the state the real service leaves behind for one of the
    three answers a submission can give (D3's taxonomy), so what is exercised is
    the script reading that state back — which is the whole of how it decides
    whether money moved.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.calls: list[str] = []

    def _bind(self, effect: Any) -> None:
        async def _uphold(dispute_id: str) -> None:
            self.calls.append(dispute_id)
            await effect(dispute_id)

        self._monkeypatch.setattr(dispute_svc, "uphold_dispute", _uphold, raising=False)

    def _writes(self, status: DisputeStatus, refund_tx: str | None) -> Any:
        async def _effect(dispute_id: str) -> None:
            await dispute_store.get_dispute_store().append_status(dispute_id, status, refund_tx=refund_tx)

        return _effect

    def lands(self, tx: str = REFUND_TX) -> None:
        """SUCCESS — the dispute ends `credited`, carrying the hash that paid it."""
        self._bind(self._writes("credited", tx))

    def lands_without_a_hash(self) -> None:
        """`credited` with nothing a reviewer could open — a state the store
        permits and the script must not report as evidence."""
        self._bind(self._writes("credited", None))

    def times_out(self, tx: str | None = REFUND_TX) -> None:
        """TIMEOUT — the claim stays held, the dispute stays `crediting`, and the
        in-flight hash is recorded for the human who reconciles it (D3)."""
        self._bind(self._writes("crediting", tx))

    def fails(self) -> None:
        """FAILED — nothing moved, so the claim was released and the dispute is
        back at `upheld`, payable again once the cause is fixed."""
        self._bind(self._writes("upheld", None))

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
    for name, value in secrets.items():
        monkeypatch.setattr(settings, name, value)
    return secrets


def test_a_landed_credit_prints_the_hash_the_explorer_url_and_the_new_status(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """The acceptance criterion, in the one form a grant reviewer can check:
    a hash, a full Stellar Expert URL, and the dispute's new status — plus the
    funding disclosure beside them, so the hash is never quoted bare."""
    uphold.lands()
    seed()

    code, out = invoke(capsys, "--dispute-id", DISPUTE_ID)

    assert code == uphold_dispute.EXIT_OK
    assert uphold.calls == [DISPUTE_ID]
    assert f"CREDITED — {CREDITABLE_USDC:.7f} USDC paid to {PAYER}" in out
    assert "status:    credited" in out
    assert f"tx:        {REFUND_TX}" in out
    assert f"https://stellar.expert/explorer/testnet/tx/{REFUND_TX}" in out
    assert "The disputed agent was NOT charged" in out


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
    assert "DO NOT RE-RUN THIS SCRIPT FOR THIS DISPUTE." in out
    assert "STILL HELD" in out
    assert f"https://stellar.expert/explorer/testnet/tx/{REFUND_TX}" in out
    assert f"https://stellar.expert/explorer/testnet/account/{PAYER}" in out
    assert "release_refund_claim" in out
    assert "FAILED" not in out.split("TIMED OUT")[0]


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
    assert "DO NOT RE-RUN THIS SCRIPT FOR THIS DISPUTE." in out


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


# ── no line of output can carry a secret ───────────────────────────────────


def test_no_line_of_a_live_run_contains_the_signing_key_or_the_api_key(
    capsys: pytest.CaptureFixture[str], credit: CreditSeam, uphold: UpholdSeam, configured: dict[str, str]
) -> None:
    """The whole of a successful run, checked against the two credentials this
    process holds. Both streams: a key on stderr is as published as one on
    stdout once the terminal is screenshotted into an evidence bundle."""
    uphold.lands()
    seed()

    assert uphold_dispute.main(["--dispute-id", DISPUTE_ID]) == uphold_dispute.EXIT_OK

    captured = capsys.readouterr()
    for value in configured.values():
        assert value not in captured.out
        assert value not in captured.err


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
    credit: CreditSeam,
    uphold: UpholdSeam,
    configured: dict[str, str],
) -> None:
    """The structural half of the guarantee: with `say` diverted, the script
    prints nothing at all.

    A test that only checked the current output for secrets would pass for as
    long as nobody added a bare `print`. This one fails the moment somebody
    does, on the preview and on the report path alike, which is the difference
    between a promise about today's lines and one about tomorrow's.
    """
    printed: list[str] = []
    monkeypatch.setattr(uphold_dispute, "say", lambda line="": printed.append(line))

    seed()
    assert uphold_dispute.main(["--dispute-id", DISPUTE_ID, "--dry-run"]) == uphold_dispute.EXIT_OK
    uphold.lands()
    assert uphold_dispute.main(["--dispute-id", DISPUTE_ID]) == uphold_dispute.EXIT_OK

    assert printed
    assert capsys.readouterr().out == ""
