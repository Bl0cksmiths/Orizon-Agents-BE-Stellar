"""A paid run that writes no rating says so, and says why.

`_submit_ratings` runs only on wallet-authorized runs. It used to return in
silence when the config was incomplete, and to trace a failed submit as
"reputation submit failed for <name>" with no reason — so a ledger stuck at
zero ratings looked, from every surface a buyer or QA could read, exactly
like a ledger nobody had paid to rate. These pin the replacement: a skipped
run traces the reason and warns the operator, a rejected rating is named by
the ledger's own error, and nothing the buyer can read carries exception
text. Only the submit is stubbed; nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

import pytest
from stellar_sdk import Keypair

from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan, TraceLine
from app.services import execution_svc
from app.services import rating_writer as rw
from app.state import state
from app.stellar import client as sc

PAYER = Keypair.from_raw_ed25519_seed(b"\x0d" * 32).public_key
JOB_ID = b"\x05" * 16
WRITER_LOG = "app.services.rating_writer"


@pytest.fixture(autouse=True)
def fresh_skip_history(monkeypatch):
    monkeypatch.setattr(rw, "_skip_warned_at", {})
    monkeypatch.setattr(rw, "_skips_unreported", {})


def _configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "C" + "A" * 55)
    monkeypatch.setattr(settings, "stellar_signing_key", "S-present")


def _plan() -> StoredPlan:
    return StoredPlan(
        id="pln_rating_trace",
        intent="rate something",
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id="agt_x",
                    agent_name="w.x",
                    rationale="r",
                    est_price_usdc=0.05,
                    est_eta_seconds=1.0,
                )
            ]
        ),
        total_usdc=0.05,
        total_eta=1.0,
    )


def _rate() -> list[TraceLine]:
    task_id = f"tsk_{secrets.token_hex(4)}"
    asyncio.run(
        execution_svc._submit_ratings(
            task_id,
            time.monotonic(),
            _plan(),
            {"w.x": {"artifact": {"title": "x"}, "critic_violations": []}},
            payer=PAYER,
            job_id=JOB_ID,
        )
    )
    return state.traces.get(task_id, [])


def _never_called(*_args, **_kwargs):  # pragma: no cover - must never run
    raise AssertionError("a rating was submitted by a deployment that cannot write one")


# ── a skipped run is no longer silent ───────────────────────────


@pytest.mark.parametrize(
    ("setting", "value", "reason", "problem"),
    [
        ("reputation_enabled", False, "reputation is disabled", "REPUTATION_ENABLED is false"),
        ("stellar_reputation_ledger", "", "no reputation ledger is configured", "STELLAR_REPUTATION_LEDGER is unset"),
        ("stellar_signing_key", "", "no signing key is configured", "STELLAR_SIGNING_KEY is unset"),
    ],
)
def test_a_skipped_run_traces_why_and_warns_the_operator(monkeypatch, caplog, setting, value, reason, problem):
    _configured(monkeypatch)
    monkeypatch.setattr(settings, setting, value)
    monkeypatch.setattr(sc, "submit_rating_async", _never_called)
    with caplog.at_level(logging.WARNING, logger=WRITER_LOG):
        lines = _rate()
    assert [(ln.level, ln.msg) for ln in lines] == [("error", f"ratings not submitted: {reason}")]
    warnings = [r.getMessage() for r in caplog.records if r.name == WRITER_LOG]
    assert len(warnings) == 1 and problem in warnings[0]
