"""Story 5.01 AC5: a mid-workflow endpoint timeout degrades cleanly on escrow v2.

    Given an external agent that stops responding partway through a workflow,
    when the workflow completes, then the buyer is charged only for delivered
    steps and the workflow still seals.

Every line of the run loop, the payout builder, the settle, the seal, the
settlement record and the rating submit is real. Only three things are
replaced, all at the edge of the process:

  * the operators' HTTP endpoints — each agent is a real `ExternalHttpWorker`
    over an `httpx.MockTransport`, so the hang is judged by the worker's own
    dispatch deadline and fails as `response_timeout`, exactly as the
    reference agent's `FAULT_MODE=hang_after:0` makes it fail on testnet;
  * the chain — `test_settle_v2._Chain` at the client seam;
  * the ReputationLedger submit.

The contract's half of the remainder is not re-proved here: `settle` returns
`max_amount - Σ payouts` to the payer itself (payment-escrow lib.rs, `let
returned = auth.max_amount - sum`). What the backend owns is that Σ payouts is
the delivered steps and nothing else, which is what these tests pin.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from stellar_sdk import scval
from test_settle_v2 import (
    AUTH_ID_HEX,
    PAYER,
    SEAL_TX,
    SETTLE_TX,
    _auth,
    _Chain,
    _install,
    _payouts,
    _plan,
    _receipt,
)

from app.agents.workers import external_http as eh
from app.config import settings
from app.schemas import Task
from app.services import dispute_store, execution_svc, reputation_svc
from app.services.dispute_store import InMemoryDisputeStore, SettlementRecord
from app.state import state
from app.stellar import client as sc

DELIVERS = "ext_delivers"
HANGS = "ext_hangs"
PRICES = {DELIVERS: 0.03, HANGS: 0.05}
# The reference agent's answer: an artifact with real content and an empty
# critic list, so an untrusted step carries checkable work.
DELIVERED = {
    "summary": "wrote the page",
    "artifact": {"title": "Page", "files": [{"path": "index.html", "content": "<h1>hi</h1>"}]},
    "critic_violations": [],
}
# 70 base + 15 artifact + 10 clean critic (reputation_svc.synthetic_rating).
DELIVERED_RATING = 95
FAILED_RATING = 20


@pytest.fixture(autouse=True)
def _execute_time_recheck_passes(monkeypatch):
    """The execute-time listing/floor gate is not what these tests pin."""
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    for key in [t for t in state.traces if t.startswith("tsk_ac5_")]:
        state.traces.pop(key, None)
    for key in [t for t in state.tasks if t.startswith("tsk_ac5_")]:
        state.tasks.pop(key, None)


class _Store(InMemoryDisputeStore):
    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[SettlementRecord] = []

    async def record_settlement(self, record: SettlementRecord) -> None:
        self.recorded.append(record)
        await super().record_settlement(record)


@pytest.fixture()
def store(monkeypatch) -> _Store:
    fresh = _Store()
    monkeypatch.setattr(dispute_store, "_store", fresh)
    return fresh


@pytest.fixture()
def ratings(monkeypatch) -> list[tuple[str, bytes, int, str]]:
    """Every rating the run submits, as (agent_id, job_id, rating, kind)."""
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "C" + "L" * 55)
    submitted: list[tuple[str, bytes, int, str]] = []

    async def submit(agent_id, job_id, rating, weight, payer, kind="auto"):
        assert payer == PAYER
        submitted.append((agent_id, job_id, rating, kind))
        return {"hash": "4a" * 32, "status": "SUCCESS"}

    monkeypatch.setattr(sc, "submit_rating_async", submit)
    monkeypatch.setattr(reputation_svc, "invalidate_rep", lambda agent_id: None)
    return submitted


def _operator(agent_id: str, *, hangs: bool, answer: dict[str, Any] | None = None) -> eh.ExternalHttpWorker:
    """A bound operator endpoint: one that answers, or one that holds the
    request open and never does (the reference agent's `hang_after:0`)."""

    async def handle(_request: httpx.Request) -> httpx.Response:
        if hangs:
            await asyncio.sleep(60)
        return httpx.Response(200, json=answer or DELIVERED)

    return eh.ExternalHttpWorker(
        agent_id,
        f"external.{agent_id}",
        f"https://{agent_id.replace('_', '-')}.example/run",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


def _bind(monkeypatch, workers: dict[str, eh.ExternalHttpWorker]) -> None:
    # A short dispatch deadline; the mechanism is the 100 s one's.
    monkeypatch.setattr(eh, "DISPATCH_DEADLINE_SECONDS", 0.2)

    async def resolve(agent_id: str) -> eh.ExternalHttpWorker:
        return workers[agent_id]

    monkeypatch.setattr(execution_svc, "resolve_worker", resolve)


def _execute(order: tuple[str, ...], task_id: str) -> None:
    plan = _plan(tuple(PRICES[a] for a in order), order)
    state.add_task(Task(id=task_id, intent=plan.intent, agents=len(order), spent=0.0, status="running"))
    asyncio.run(
        execution_svc._run(
            plan,
            task_id,
            auth_id_hex=AUTH_ID_HEX,
            payer=PAYER,
            authorized_max=sc.usdc_to_i128(plan.total_usdc),
        )
    )


def _messages(task_id: str) -> list[str]:
    return [line.msg for line in state.traces[task_id]]


# ── one step delivers, one endpoint hangs ───────────────────────────────
@pytest.mark.parametrize(
    "order",
    [(DELIVERS, HANGS), (HANGS, DELIVERS)],
    ids=["timeout-last", "timeout-first"],
)
def test_a_hung_endpoint_is_not_charged_and_the_workflow_still_seals(monkeypatch, store, ratings, order):
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    _bind(
        monkeypatch,
        {DELIVERS: _operator(DELIVERS, hangs=False), HANGS: _operator(HANGS, hangs=True)},
    )
    task_id = "tsk_ac5_" + "_".join(order)
    delivered_at, hung_at = order.index(DELIVERS), order.index(HANGS)

    _execute(order, task_id)

    # The hang failed as the worker's own deadline, not the run loop's.
    assert f"external.{HANGS} failed (response_timeout)" in _messages(task_id)

    # Charged only for the delivered step: one payout, at its own price. The
    # authorization held both prices, so the settle returns the hung step's
    # share to the buyer (max_amount - Σ payouts).
    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": DELIVERS, "amount": 300_000}]
    authorized = sc.usdc_to_i128(sum(PRICES.values()))
    assert authorized - sum(p["amount"] for p in _payouts(settle)) == sc.usdc_to_i128(PRICES[HANGS])
    assert any("the rest released" in m for m in _messages(task_id))

    # Sealed, under the settle's own job id: ONLY the agent that delivered and
    # was paid (D-086 — the hung one is not attested to work it never did),
    # its receipt beside it, and the paid total, not the plan's.
    [seal] = chain.named("seal")
    job_id = scval.to_native(settle[2])
    assert scval.to_native(seal[1]) == job_id
    assert scval.to_native(seal[4]) == [DELIVERS]
    assert scval.to_native(seal[5]) == [_receipt(0)]
    assert scval.to_native(seal[6]) == 300_000
    assert any(m.startswith("workflow sealed — 1 agents · 0.030 USDC") for m in _messages(task_id))

    # The settlement a dispute is judged against says the same thing.
    record = store.recorded[-1]
    assert record.proof_tx == SEAL_TX and record.settled_usdc == pytest.approx(PRICES[DELIVERS])
    by_step = {s.step_index: s for s in record.steps}
    assert (by_step[delivered_at].delivered, by_step[delivered_at].paid_usdc) == (True, PRICES[DELIVERS])
    assert by_step[delivered_at].receipt_id_hex == _receipt(0).hex()
    assert (by_step[hung_at].delivered, by_step[hung_at].paid_usdc, by_step[hung_at].receipt_id_hex) == (
        False,
        0.0,
        None,
    )

    # Both steps rated, each under its own step's id derived from the job:
    # the hung one 20/100, the delivered one on its evidence.
    expected = {
        delivered_at: (DELIVERS, DELIVERED_RATING),
        hung_at: (HANGS, FAILED_RATING),
    }
    assert ratings == [
        (expected[i][0], execution_svc.settlement_job_id(job_id, i), expected[i][1], "auto") for i in (0, 1)
    ]

    # What the buyer sees: the run completed (the delivered step shipped the
    # artifact), priced at what was paid, with both hashes on the task.
    task = state.tasks[task_id]
    assert (task.status, task.spent, task.settlement) == ("complete", PRICES[DELIVERS], "settled")
    assert (task.charge_tx, task.proof_tx) == (SETTLE_TX, SEAL_TX)
    assert task.artifact is not None and task.artifact["title"] == "Page"
    assert [f["path"] for f in task.artifact["files"]] == ["index.html"]


def test_a_partial_run_with_no_artifact_left_is_failed_yet_charged_and_sealed(monkeypatch, store, ratings):
    """`_terminal_status`: a partial run is judged on its deliverable. The one
    step that answered shipped no artifact, so the badge says failed — but it
    DID deliver checkable work, so it is paid, and the run is sealed and
    disputable exactly as a complete one is."""
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    no_artifact = {"summary": "reviewed it", "critic_violations": []}
    _bind(
        monkeypatch,
        {DELIVERS: _operator(DELIVERS, hangs=False, answer=no_artifact), HANGS: _operator(HANGS, hangs=True)},
    )

    _execute((DELIVERS, HANGS), "tsk_ac5_no_artifact")

    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": DELIVERS, "amount": 300_000}]
    [seal] = chain.named("seal")
    assert scval.to_native(seal[4]) == [DELIVERS]
    assert [(agent, rating) for agent, _job, rating, _kind in ratings] == [(DELIVERS, 80), (HANGS, FAILED_RATING)]
    task = state.tasks["tsk_ac5_no_artifact"]
    assert (task.status, task.spent, task.settlement) == ("failed", PRICES[DELIVERS], "settled")
    assert (task.charge_tx, task.proof_tx) == (SETTLE_TX, SEAL_TX)
    assert "workflow incomplete — 1/2 agents produced output" in _messages("tsk_ac5_no_artifact")


# ── every endpoint hangs ────────────────────────────────────────────────
def test_when_every_step_times_out_nothing_is_paid_or_sealed_and_custody_goes_back(monkeypatch, store, ratings):
    """The rule for a run with no delivered step: an EMPTY settle, which pays
    nobody and returns the whole custody to the buyer now, under the task's
    derived job id; no seal and no settlement record, because there is no
    delivered work to attest to or dispute; and every step still rated 20."""
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    _bind(monkeypatch, {DELIVERS: _operator(DELIVERS, hangs=True), HANGS: _operator(HANGS, hangs=True)})
    task_id = "tsk_ac5_all_hung"

    _execute((DELIVERS, HANGS), task_id)

    [settle] = chain.named("settle")
    assert _payouts(settle) == []
    unsettled = execution_svc.unsettled_job_id(task_id)
    assert scval.to_native(settle[2]) == unsettled
    assert chain.named("seal") == []
    assert store.recorded == []
    assert ratings == [
        (DELIVERS, execution_svc.settlement_job_id(unsettled, 0), FAILED_RATING, "auto"),
        (HANGS, execution_svc.settlement_job_id(unsettled, 1), FAILED_RATING, "auto"),
    ]
    task = state.tasks[task_id]
    assert (task.status, task.spent, task.settlement) == ("failed", 0.0, "released")
    # The release's own hash stands where a charge would; nothing was attested.
    assert (task.charge_tx, task.proof_tx) == (SETTLE_TX, None)
    assert any("custody released to the buyer" in m for m in _messages(task_id))
