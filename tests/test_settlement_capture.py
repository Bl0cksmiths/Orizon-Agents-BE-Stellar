"""Story 4.02 capture — a paid workflow must leave behind the record its
dispute is judged against.

None of it was persisted before. The job id was a local in `_settle_onchain`,
the payer a parameter of `_run`, and `state.tasks` carries no per-step price, no
link back to the plan and no settlement time — while evicting finished tasks
first and losing everything on restart, which is exactly the set and exactly the
moment a buyer disputes. These tests pin the four facts a dispute needs — WHO
paid, WHICH job, WHAT each step cost and whether it delivered, and UNTIL WHEN —
plus the rule about when not to write at all: no charge landed, so no money
moved, so there is nothing to dispute.

Hermetic like the rest of the suite: `_settle_onchain` and `_submit_ratings` are
patched out at the seam, so nothing here reaches the network.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from stellar_sdk import Keypair

from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import dispute_store, execution_svc
from app.services.dispute_store import InMemoryDisputeStore, SettlementRecord
from app.state import state

AUTH_ID_HEX = "ab" * 16
PAYER = Keypair.random().public_key
CHARGE_TX = "chargehash123"
PROOF_TX = "sealhash456"
# The 16-byte job id a successful charge mints, as `_settle_onchain` returns it.
JOB_ID = b"\x7f" * 16


@pytest.fixture(autouse=True)
def clean_state():
    yield
    for task_id in [t for t in state.traces if t.startswith("tsk_capture_")]:
        state.traces.pop(task_id, None)
    for task_id in [t for t in state.tasks if t.startswith("tsk_capture_")]:
        state.tasks.pop(task_id, None)


class _RecordingStore(InMemoryDisputeStore):
    """The real in-memory store, plus the order it was written in.

    Subclassed rather than faked so the tests exercise the store the service
    actually calls, and `recorded` answers the one question the store's own API
    cannot: how MANY times a single run wrote.
    """

    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[SettlementRecord] = []

    async def record_settlement(self, record: SettlementRecord) -> None:
        self.recorded.append(record)
        await super().record_settlement(record)


@pytest.fixture()
def store(monkeypatch) -> _RecordingStore:
    """A cold store for this test, installed as the process singleton.

    Patched at `dispute_store._store` rather than at the service's imported
    `get_dispute_store`, so the accessor under test is the real one and the
    singleton is restored for whatever runs next.
    """
    fresh = _RecordingStore()
    monkeypatch.setattr(dispute_store, "_store", fresh)
    return fresh


class _OkWorker:
    def __init__(self, name: str = "w.ok") -> None:
        self.name = name

    async def run(self, intent, rationale, context=None):
        return {"summary": "did the thing"}


class _BoomWorker:
    def __init__(self, name: str = "w.boom") -> None:
        self.name = name

    async def run(self, intent, rationale, context=None):
        raise RuntimeError("openai: invalid_api_key")


class _FlakyWorker:
    """Fails its first step and delivers on its second — the same agent twice
    in one plan, which is the case a settlement keyed by agent_id gets wrong."""

    name = "w.flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, intent, rationale, context=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("openai: rate_limited")
        return {"summary": "second time lucky"}


def _plan(prices: tuple[float, ...] = (0.05,), agent_ids: tuple[str, ...] | None = None) -> StoredPlan:
    ids = agent_ids or tuple(f"agt_{i}" for i in range(len(prices)))
    return StoredPlan(
        id="pln_capture",
        intent="settle something",
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id=agent_id,
                    agent_name=f"w.{agent_id}",
                    rationale="r",
                    est_price_usdc=price,
                    est_eta_seconds=1.0,
                )
                for agent_id, price in zip(ids, prices, strict=True)
            ]
        ),
        total_usdc=sum(prices),
        total_eta=1.0,
    )


def _add_task(task_id: str, agents: int = 1) -> None:
    state.add_task(
        Task(
            id=task_id,
            intent="settle something",
            agents=agents,
            spent=0.0,
            status="running",
            started="just now",
        )
    )


def _resolves_to(monkeypatch, lookup) -> None:
    async def _resolve(agent_id):
        return lookup(agent_id)

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)


def _patch_settlement(
    monkeypatch,
    *,
    charge_tx: str | None = CHARGE_TX,
    proof_tx: str | None = PROOF_TX,
    job_id: bytes | None = JOB_ID,
) -> list[bytes]:
    """Pin the on-chain seam to a fixed outcome; collect the rating job ids.

    The returned list is what proves the workflow carried on past the recorder:
    ratings run after it, so an empty list means `_run` never got there.
    """
    rating_calls: list[bytes] = []

    async def fake_settle(task_id, start, plan, *, payer, auth_id_hex, total_usdc):
        return (charge_tx, proof_tx, job_id)

    async def fake_ratings(
        task_id, start, plan, delivered, *, payer, job_id, undispatched=frozenset(), first_party_ids=frozenset()
    ):
        rating_calls.append(job_id)

    monkeypatch.setattr(execution_svc, "_settle_onchain", fake_settle)
    monkeypatch.setattr(execution_svc, "_submit_ratings", fake_ratings)
    return rating_calls


def _run_paid(plan: StoredPlan, task_id: str) -> None:
    _add_task(task_id, agents=len(plan.plan.steps))
    asyncio.run(execution_svc._run(plan, task_id, auth_id_hex=AUTH_ID_HEX, payer=PAYER))


def _errors(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.name == "app.services.execution_svc" and r.levelno >= logging.ERROR
    ]


# ── a settled run leaves exactly one record ─────────────────────────────
def test_settled_run_records_the_facts_a_dispute_needs(monkeypatch, store):
    _resolves_to(monkeypatch, lambda agent_id: _OkWorker())
    _patch_settlement(monkeypatch)
    task_id = "tsk_capture_ok"
    before = time.time()

    _run_paid(_plan((0.05, 0.05)), task_id)

    assert len(store.recorded) == 1, "a settlement must be written once per settled run"
    record = store.recorded[0]
    assert record.task_id == task_id
    assert record.payer == PAYER
    assert record.auth_id_hex == AUTH_ID_HEX
    # The job id the charge minted — the id a dispute is filed against, and
    # the one value that existed nowhere but a log line before this story.
    assert record.job_id_hex == JOB_ID.hex()
    assert (record.charge_tx, record.proof_tx) == (CHARGE_TX, PROOF_TX)
    # Reachable both ways round: by job for the dispute route, by task for the
    # buyer looking at the run they just watched.
    assert asyncio.run(store.get_settlement(JOB_ID.hex())) == record
    assert asyncio.run(store.get_settlement_by_task(task_id)) == record
    # Epoch seconds from our own clock, not a monotonic offset that means
    # nothing across a restart.
    assert before <= record.settled_at <= time.time()


def test_window_closes_the_configured_seconds_after_settlement(monkeypatch, store):
    """The promise is stamped, not recomputed: `window_closes_at` is fixed to
    the setting in force AT SETTLEMENT, so retuning it later cannot move a
    deadline the buyer was already given."""
    monkeypatch.setattr(settings, "dispute_window_seconds", 3_600.0)
    _resolves_to(monkeypatch, lambda agent_id: _OkWorker())
    _patch_settlement(monkeypatch)
    task_id = "tsk_capture_window"

    _run_paid(_plan(), task_id)

    record = store.recorded[0]
    assert record.window_closes_at == pytest.approx(record.settled_at + 3_600.0)

    monkeypatch.setattr(settings, "dispute_window_seconds", 999_999.0)
    assert record.window_closes_at == pytest.approx(record.settled_at + 3_600.0)


def test_trace_tells_the_buyer_the_window_is_open(monkeypatch, store):
    """The window is a promise made to the buyer, so it belongs in the buyer's
    own record of the run — with its closing time, and without the job id,
    which is the dispute's handle and the trace is world-readable when
    TASK_AUTH_REQUIRED is off."""
    monkeypatch.setattr(settings, "dispute_window_seconds", 7_200.0)
    _resolves_to(monkeypatch, lambda agent_id: _OkWorker())
    _patch_settlement(monkeypatch)
    task_id = "tsk_capture_trace"

    _run_paid(_plan(), task_id)

    closes = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(store.recorded[0].window_closes_at))
    lines = state.traces[task_id]
    assert any("dispute window" in ln.msg and closes in ln.msg for ln in lines), [ln.msg for ln in lines]
    assert not any(JOB_ID.hex() in ln.msg for ln in lines)


# ── the amount is what moved, and the steps are what was bought ─────────
def test_settled_amount_is_what_moved_not_what_was_estimated(monkeypatch, store):
    """`spent` is a sum of the plan's ESTIMATES; the charge sends
    `usdc_to_i128(max(total, 0.000001))`, which rounds to the ledger's 7
    decimals. A credit computed from the estimate would therefore hand back
    more than ever left escrow, so the record carries the charged number."""
    _resolves_to(monkeypatch, lambda agent_id: _OkWorker())
    _patch_settlement(monkeypatch)
    task_id = "tsk_capture_amount"

    _run_paid(_plan((0.050000049,)), task_id)

    record = store.recorded[0]
    # round(0.050000049 * 10**7) = 500_000 stroops = 0.05 USDC exactly.
    assert record.settled_usdc == pytest.approx(0.05)
    assert record.settled_usdc < 0.050000049
    assert record.settled_usdc < sum(s.price_usdc for s in record.steps)


def test_a_free_plan_records_the_dust_the_charge_floors_to(monkeypatch, store):
    """`max(total_usdc, 0.000001)`: a zero-priced plan still moves one dust
    unit on-chain, so the ceiling on any credit is dust, not zero."""
    _resolves_to(monkeypatch, lambda agent_id: _OkWorker())
    _patch_settlement(monkeypatch)
    task_id = "tsk_capture_dust"

    _run_paid(_plan((0.0,)), task_id)

    assert store.recorded[0].settled_usdc == pytest.approx(0.000001)


def test_every_plan_step_is_recorded_with_its_price_and_delivery(monkeypatch, store):
    """All three steps are recorded in the plan's order — a dispute is filed
    against a step index — but only the one that produced output is disputable:
    the second failed and the third never resolved to a worker at all, and
    neither was billed."""
    workers = {"agt_0": _OkWorker("w.gen"), "agt_1": _BoomWorker("w.critic")}
    _resolves_to(monkeypatch, workers.get)
    _patch_settlement(monkeypatch)
    task_id = "tsk_capture_steps"

    _run_paid(_plan((0.05, 0.02, 0.01)), task_id)

    steps = store.recorded[0].steps
    assert [s.step_index for s in steps] == [0, 1, 2]
    assert [s.agent_id for s in steps] == ["agt_0", "agt_1", "agt_2"]
    assert [s.agent_name for s in steps] == ["w.agt_0", "w.agt_1", "w.agt_2"]
    assert [s.price_usdc for s in steps] == [pytest.approx(0.05), pytest.approx(0.02), pytest.approx(0.01)]
    assert [s.delivered for s in steps] == [True, False, False]
    # Only the delivered step was billed, so only its price was charged.
    assert store.recorded[0].settled_usdc == pytest.approx(0.05)


def test_a_repeated_agent_is_judged_per_step_not_per_agent(monkeypatch, store):
    """One agent, twice in a plan, failing then delivering. Keyed by agent id
    the failed step would be recorded as delivered on the strength of the
    later one, and 4.02 would accept a dispute over unpaid work."""
    worker = _FlakyWorker()
    _resolves_to(monkeypatch, lambda agent_id: worker)
    _patch_settlement(monkeypatch)
    task_id = "tsk_capture_dup"

    _run_paid(_plan((0.05, 0.05), agent_ids=("agt_dup", "agt_dup")), task_id)

    assert [s.delivered for s in store.recorded[0].steps] == [False, True]
