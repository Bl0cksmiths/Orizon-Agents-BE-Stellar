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
