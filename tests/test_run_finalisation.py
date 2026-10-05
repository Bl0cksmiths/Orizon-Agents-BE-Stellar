"""A paid run's task reflects its settlement the moment money moves.

After a v2 `settle` confirms, a run still has a seal to submit (one on-chain
poll) and then one rating per step, submitted one after another, each waiting
up to ~30 s for its own poll. The buyer's receipt used to read `running` for
all of it — minutes, after their money had already moved. These tests pin the
order now: the task is terminal, with the settle's hash on it, as soon as the
settle confirms; the seal's hash follows when the seal confirms; and the
ratings are follow-up work the run still owns (counted against capacity,
traced, flushed) but no longer holds the receipt hostage to.

Nothing about the money path moves with it: the settlement is recorded where
it always was, the dispute window opens where it always did, and a run
cancelled after the settle no longer overwrites what it paid with `failed` and
no hash.

Hermetic: the chain is `test_settle_v2._Chain`, with the seal and the rating
submits held open on events so each test can look at the task mid-flight.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from test_settle_v2 import (
    AUTH_ID_HEX,
    PAYER,
    SEAL_TX,
    SETTLE_TX,
    _auth,
    _Chain,
    _install,
    _Ok,
    _plan,
    _Store,
)

from app.config import settings
from app.schemas import Task
from app.services import dispute_store, execution_svc, reputation_svc
from app.state import state
from app.stellar import client as sc


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)
    yield
    for key in [t for t in state.tasks if t.startswith("tsk_fin_")]:
        state.tasks.pop(key, None)
        state.traces.pop(key, None)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fresh = _Store()
    monkeypatch.setattr(dispute_store, "_store", fresh)
    return fresh


class _HeldSeal(_Chain):
    """`_Chain` whose seal does not answer until `release` is set."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sealing = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, contract_id: str, function_name: str, args: list[Any]) -> dict[str, Any]:
        if function_name == "seal":
            self.sealing.set()
            await self.release.wait()
        return await super().invoke(contract_id, function_name, args)


def _workers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(agent_id: str) -> _Ok:
        return _Ok()

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)


def _held_ratings(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event, list[str]]:
    """Ratings on, each submit held until `release`; `rating` is set when the first one starts."""
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "C" + "L" * 55)
    monkeypatch.setattr(reputation_svc, "invalidate_rep", lambda agent_id: None)
    rating, release = asyncio.Event(), asyncio.Event()
    submitted: list[str] = []

    async def submit(agent_id: str, *_a: Any, **_k: Any) -> dict[str, Any]:
        rating.set()
        await release.wait()
        submitted.append(agent_id)
        return {"hash": "4a" * 32, "status": "SUCCESS"}

    monkeypatch.setattr(sc, "submit_rating_async", submit)
    return rating, release, submitted


def _start(task_id: str, prices: tuple[float, ...] = (0.01, 0.02)) -> asyncio.Task[None]:
    state.add_task(Task(id=task_id, intent="build a thing", agents=len(prices), spent=0.0, status="running"))
    return asyncio.create_task(
        execution_svc._run(
            _plan(prices), task_id, auth_id_hex=AUTH_ID_HEX, payer=PAYER, authorized_max=sc.usdc_to_i128(sum(prices))
        )
    )


async def _until(flag: asyncio.Event | Callable[[], bool], what: str) -> None:
    async def wait() -> None:
        if isinstance(flag, asyncio.Event):
            await flag.wait()
            return
        while not flag():
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(wait(), timeout=5)
    except TimeoutError:
        raise AssertionError(f"timed out waiting for {what}") from None


def _run(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(scenario())


def test_the_task_is_settled_and_complete_while_the_seal_is_still_in_flight(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    async def scenario() -> None:
        chain = _install(monkeypatch, _HeldSeal(auth=_auth()))
        _workers(monkeypatch)
        run = _start("tsk_fin_seal")
        await _until(chain.sealing, "the seal to be submitted")

        mid = state.tasks["tsk_fin_seal"]
        assert (mid.status, mid.settlement, mid.charge_tx, mid.proof_tx) == ("complete", "settled", SETTLE_TX, None)
        assert mid.spent == pytest.approx(0.03)
        # The dispute window opened at the settle, exactly where it always did.
        assert store.recorded and store.recorded[0].proof_tx is None

        chain.release.set()
        await run
        done = state.tasks["tsk_fin_seal"]
        assert (done.status, done.charge_tx, done.proof_tx) == ("complete", SETTLE_TX, SEAL_TX)

    _run(scenario)


def test_ratings_run_after_the_task_is_final(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    async def scenario() -> None:
        _install(monkeypatch, _Chain(auth=_auth()))
        _workers(monkeypatch)
        rating, release, submitted = _held_ratings(monkeypatch)
        run = _start("tsk_fin_ratings")
        await _until(rating, "the first rating submit")

        mid = state.tasks["tsk_fin_ratings"]
        assert (mid.status, mid.settlement, mid.charge_tx, mid.proof_tx) == ("complete", "settled", SETTLE_TX, SEAL_TX)
        # Still the run's own work, not yet finished.
        assert not run.done()

        release.set()
        await run
        assert submitted == ["agt_0", "agt_1"]
        assert [line.msg for line in state.traces["tsk_fin_ratings"] if line.msg.startswith("reputation →")]

    _run(scenario)


def test_a_run_cancelled_after_its_settle_keeps_what_it_paid(monkeypatch: pytest.MonkeyPatch, store: _Store) -> None:
    """Shutdown lands during the seal: the money moved, so the task must not be
    rewritten as `failed` with no hash — the receipt is the buyer's evidence."""

    async def scenario() -> None:
        chain = _install(monkeypatch, _HeldSeal(auth=_auth()))
        _workers(monkeypatch)
        run = _start("tsk_fin_cancel")
        await _until(chain.sealing, "the seal to be submitted")
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    _run(scenario)
    task = state.tasks["tsk_fin_cancel"]
    assert (task.status, task.settlement, task.charge_tx) == ("complete", "settled", SETTLE_TX)
    assert "workflow cancelled" in [line.msg for line in state.traces["tsk_fin_cancel"]]


def test_a_run_whose_settle_did_not_confirm_is_final_before_its_ratings(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    """No money moved (the ledger rejected the settle), and the run is still
    over: the receipt says so before the ratings, not after them."""

    async def scenario() -> None:
        _install(monkeypatch, _Chain(auth=_auth(), settle={"hash": SETTLE_TX, "status": "FAILED"}))
        _workers(monkeypatch)
        rating, release, _submitted = _held_ratings(monkeypatch)
        run = _start("tsk_fin_rejected")
        await _until(rating, "the first rating submit")
        mid = state.tasks["tsk_fin_rejected"]
        assert (mid.status, mid.settlement, mid.charge_tx) == ("complete", "failed", None)
        release.set()
        await run

    _run(scenario)


def test_a_stream_opened_after_settlement_follows_the_run_to_its_end(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    """The task is `complete` from the settle on, but the run is still writing
    — the seal, the ratings. A viewer who opens the stream in that stretch gets
    the history AND the lines still to come, and `done` only when the run
    closes the stream, not the moment the status turned terminal."""
    from app.routers import trace as trace_router

    async def scenario() -> None:
        _install(monkeypatch, _Chain(auth=_auth()))
        _workers(monkeypatch)
        rating, release, _submitted = _held_ratings(monkeypatch)
        run = _start("tsk_fin_stream")
        await _until(rating, "the first rating submit")
        assert state.tasks["tsk_fin_stream"].status == "complete"

        response = await trace_router.stream_trace("tsk_fin_stream")
        events = response.body_iterator
        history = len(state.traces["tsk_fin_stream"])
        received: list[dict[str, str]] = [await anext(events) for _ in range(history)]
        release.set()
        async for event in events:
            received.append(event)
            if event["event"] == "done":
                break
        await run
        kinds = [e["event"] for e in received]
        assert kinds[-1] == "done"
        after_history = [e["data"] for e in received[history:] if e["event"] == "trace"]
        assert any("reputation →" in data for data in after_history)

    _run(scenario)


def test_a_finished_task_no_run_here_is_writing_still_replays_and_ends(client: Any) -> None:
    """The other half of the rule: a terminal task with no live run in this
    process — finished long ago, or read back from the store — has no producer,
    so its stream must end rather than ping forever."""
    state.add_task(Task(id="tsk_fin_old", intent="x", agents=1, spent=0.0, status="complete"))
    with client.stream("GET", "/api/trace/tsk_fin_old/stream") as r:
        lines = [line for line in r.iter_lines() if line.startswith("event:")]
    assert lines == ["event: done"]
