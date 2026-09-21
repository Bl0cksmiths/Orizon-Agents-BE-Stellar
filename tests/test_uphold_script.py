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
from app.services import dispute_store
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


@pytest.fixture(autouse=True)
def _fresh_store():
    """A store per test.

    It is a process singleton, and `DATABASE_URL` in the environment of whoever
    runs the suite must not turn one of these into a live query — the hermetic
    fixture in conftest does not clear that one.
    """
    saved = settings.database_url
    settings.database_url = ""
    dispute_store._store = None
    yield
    dispute_store._store = None
    settings.database_url = saved


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
