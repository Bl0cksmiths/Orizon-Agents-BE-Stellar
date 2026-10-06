"""D-090: AppState as a hot cache in front of the durable task store.

Hermetic: the store is `FakeStore`, a dict-backed implementation of the
`task_store.TaskStore` protocol with failure injection, installed where the
persistence layer resolves the store. What the SQL does against a real
Postgres is `tests/test_task_store_postgres.py`'s; what a whole restart keeps
is `tests/test_task_restart.py`'s. This file pins the layer between: what is
written, when, how it survives a database that is down or refuses a row, how a
task this process never ran is read back and authorised, and that no request
ever hangs on the database.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from typing import Any

import pytest

from app import state as state_module
from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan, Task, TraceLine
from app.services import execution_svc, task_persistence, task_store
from app.services.task_store import PruneResult, StoredTask, TaskRow, TaskStoreRejected, WriteBatch
from app.state import AppState, read_token_digest, state

OTHER_BOOT = "0" * 16
TOKEN = "read-token-from-before-the-restart"
API_KEY = "operator-key"


class FakeStore:
    """`TaskStore`, in dicts, keeping the rows exactly as the SQL would."""

    def __init__(self) -> None:
        self.tasks: dict[str, TaskRow] = {}
        self.traces: dict[tuple[str, int], str] = {}
        self.plans: dict[str, StoredPlan] = {}
        self.batches: list[WriteBatch] = []
        self.loads: list[str] = []
        self.fail_writes = 0  # the next N writes raise a transient error
        self.refuse: set[str] = set()  # task ids whose rows the "database" refuses as data
        self.down = False  # every read raises
        self.read_delay = 0.0

    async def write(self, batch: WriteBatch) -> None:
        if self.fail_writes:
            self.fail_writes -= 1
            raise ConnectionResetError("connection was closed in the middle of operation")
        if any(t.task_id in self.refuse for t in batch.tasks):
            raise TaskStoreRejected("invalid byte sequence")
        self.batches.append(batch)
        for plan in batch.plans:
            self.plans[plan.plan_id] = StoredPlan.model_validate_json(plan.body)
        for row in batch.tasks:
            kept = self.tasks.get(row.task_id)
            digest = row.read_token_sha256 or (kept.read_token_sha256 if kept else None)
            self.tasks[row.task_id] = TaskRow(
                row.task_id, row.body, row.status, row.started_at, digest, row.boot_id, row.updated_at
            )
        for trace in batch.traces:
            self.traces.setdefault((trace.task_id, trace.seq), trace.line)

    async def load_task(self, task_id: str) -> StoredTask | None:
        self.loads.append(task_id)
        if self.read_delay:
            await asyncio.sleep(self.read_delay)
        if self.down:
            raise OSError("could not connect to server")
        row = self.tasks.get(task_id)
        if row is None:
            return None
        lines = sorted((seq, line) for (tid, seq), line in self.traces.items() if tid == task_id)
        return StoredTask(
            task=Task.model_validate_json(row.body),
            read_token_sha256=row.read_token_sha256,
            traces=tuple(TraceLine.model_validate_json(line) for _seq, line in lines),
            boot_id=row.boot_id,
            updated_at=row.updated_at,
        )

    async def load_plan(self, plan_id: str) -> StoredPlan | None:
        if self.down:
            raise OSError("could not connect to server")
        return self.plans.get(plan_id)

    async def recent_task_ids(self, limit: int) -> list[str]:
        newest = sorted(self.tasks.values(), key=lambda row: row.started_at, reverse=True)
        return [row.task_id for row in newest[: max(limit, 0)]]

    async def prune(self, *, task_cutoff: float, max_tasks: int, plan_cutoff: float) -> PruneResult:
        return PruneResult(0, 0, 0)

    async def close(self) -> None:
        return None

    # what a previous process left behind
    def seed(self, task: Task, *, lines: tuple[str, ...] = (), boot_id: str = OTHER_BOOT, age: float = 0.0) -> None:
        self.tasks[task.id] = TaskRow.of(
            task, read_token_sha256=read_token_digest(TOKEN), boot_id=boot_id, updated_at=time.time() - age
        )
        for seq, msg in enumerate(lines):
            self.traces[(task.id, seq)] = TraceLine(t=f"00.{seq:03d}", level="exec", msg=msg).model_dump_json()


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeStore]:
    """A fresh store AND a fresh journal: the suite's own journal is the
    process observer, and its queue must not leak between tests."""
    fake = FakeStore()
    monkeypatch.setattr(task_store, "get_task_store", lambda: fake)
    journal = task_persistence.TaskJournal()
    monkeypatch.setattr(task_persistence, "journal", journal)
    monkeypatch.setattr(task_persistence, "RETRY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(task_persistence, "RETRY_MAX_SECONDS", 0.02)
    state_module.set_observer(journal)
    task_persistence._misses.clear()
    task_persistence._foreign.clear()
    saved = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in vars(state).items()}
    yield fake
    vars(state).update(saved)
    state_module.set_observer(_suite_journal)
    task_persistence._misses.clear()
    task_persistence._foreign.clear()


_suite_journal = task_persistence.journal


def _id(n: int) -> str:
    return f"tsk_{n:016x}"


def _task(n: int, status: str = "complete", **extra: Any) -> Task:
    return Task(id=_id(n), intent="build a page", agents=1, spent=0.01, status=status, **extra)  # type: ignore[arg-type]


def _plan(plan_id: str = "pln_0a0b0c0d") -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent="build a page",
        plan=Plan(
            steps=[PlanStep(agent_id="agt_x", agent_name="w.x", rationale="r", est_price_usdc=0.01, est_eta_seconds=1)]
        ),
        total_usdc=0.01,
        total_eta=1.0,
    )


# ── writing ─────────────────────────────────────────────────────────────
def test_a_burst_of_writes_lands_as_one_batch_holding_the_latest_snapshot(store: FakeStore) -> None:
    async def run() -> bool:
        state.add_task(_task(1, "running"), read_token=TOKEN)
        for spent in (0.01, 0.02, 0.03):
            state.put_task(state.tasks[_id(1)].model_copy(update={"spent": spent}))
        for i in range(3):
            state.append_trace(_id(1), TraceLine(t="00.000", level="exec", msg=f"line {i}"))
        state.add_plan(_plan())
        return await task_persistence.flush(1.0)

    assert asyncio.run(run()) is True
    [batch] = store.batches
    [row] = batch.tasks
    assert Task.model_validate_json(row.body).spent == 0.03
    # The digest from the creation snapshot survives the coalescing — and the
    # token itself is nowhere in what was written.
    assert row.read_token_sha256 == read_token_digest(TOKEN)
    assert TOKEN not in repr(batch)
    assert [(t.task_id, t.seq) for t in batch.traces] == [(_id(1), 0), (_id(1), 1), (_id(1), 2)]
    assert [p.plan_id for p in batch.plans] == ["pln_0a0b0c0d"]


class _Slow:
    name = "w.slow"

    async def run(self, intent: str, rationale: str, context: Any = None) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"summary": "done"}


@pytest.fixture()
def slow_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)

    async def _resolve(agent_id: str) -> _Slow:
        return _Slow()

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)


def test_execute_answers_only_once_its_task_and_token_are_durable(
    store: FakeStore, client: Any, slow_runs: None
) -> None:
    """Write-through for the receipt: the id and token `/execute` returns are
    already in the store when the response arrives, and once the run is over
    its terminal state is too."""
    state.add_plan(_plan())

    r = client.post("/api/orchestrator/execute", json={"plan_id": "pln_0a0b0c0d"})

    task_id, token = r.json()["task_id"], r.json()["read_token"]
    assert store.tasks[task_id].read_token_sha256 == read_token_digest(token)
    deadline = time.monotonic() + 5
    while Task.model_validate_json(store.tasks[task_id].body).status == "running":
        assert time.monotonic() < deadline, "the run's terminal state never became durable"
        time.sleep(0.02)
    assert Task.model_validate_json(store.tasks[task_id].body).status == "complete"


def test_execute_plan_itself_never_waits_on_the_database(
    store: FakeStore, slow_runs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write-through wait belongs to the route, after the authorization is
    claimed for the task. Inside `execute_plan` — between starting the run and
    the router's claim — a cancelled wait would unclaim an authorization whose
    run is already spending it."""

    async def _hangs(batch: WriteBatch) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(store, "write", _hangs)

    async def run() -> float:
        began = time.monotonic()
        await execution_svc.execute_plan(_plan())
        took = time.monotonic() - began
        for t in execution_svc._background_tasks:
            if t.get_loop() is asyncio.get_running_loop():
                t.cancel()
        return took

    assert asyncio.run(run()) < 0.5


def test_a_database_that_fails_is_retried_until_it_takes_the_write(
    store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.fail_writes = 3

    async def run() -> bool:
        state.add_task(_task(2), read_token=TOKEN)
        return await task_persistence.flush(2.0)

    with caplog.at_level(logging.WARNING, logger="app.services.task_persistence"):
        assert asyncio.run(run()) is True
    assert _id(2) in store.tasks and store.tasks[_id(2)].read_token_sha256 == read_token_digest(TOKEN)
    assert sum("failed (attempt" in r.getMessage() for r in caplog.records) == 3


def test_a_database_that_stays_down_never_blocks_the_caller(store: FakeStore) -> None:
    """The run loop and the routes report and move on; `flush` gives up at its
    bound and says so; nothing raises."""
    store.fail_writes = 10**6

    async def run() -> tuple[bool, float]:
        began = time.monotonic()
        state.add_task(_task(3), read_token=TOKEN)
        state.append_trace(_id(3), TraceLine(t="00.000", level="exec", msg="still running"))
        flushed = await task_persistence.flush(0.2)
        return flushed, time.monotonic() - began

    flushed, took = asyncio.run(run())
    assert flushed is False and took < 1.0
    assert state.tasks[_id(3)].status == "complete"
    assert store.tasks == {}


def test_the_queue_is_bounded_while_the_database_is_down(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(task_persistence, "MAX_PENDING_TRACE_LINES", 5)
    store.fail_writes = 10**6
    state.add_task(_task(4))
    with caplog.at_level(logging.ERROR, logger="app.services.task_persistence"):
        for i in range(12):
            state.append_trace(_id(4), TraceLine(t="00.000", level="exec", msg=f"line {i}"))

    pending = task_persistence.journal._pending.traces
    assert [seq for _tid, seq in pending] == [7, 8, 9, 10, 11]
    assert any("write queue full" in r.getMessage() for r in caplog.records)


def test_a_row_the_database_refuses_is_dropped_alone(store: FakeStore, caplog: pytest.LogCaptureFixture) -> None:
    store.refuse = {_id(6)}

    async def run() -> bool:
        state.add_task(_task(5))
        state.add_task(_task(6))
        state.add_task(_task(7))
        return await task_persistence.flush(1.0)

    with caplog.at_level(logging.ERROR, logger="app.services.task_persistence"):
        assert asyncio.run(run()) is True
    assert set(store.tasks) == {_id(5), _id(7)}
    assert any("dropped a row the database refuses" in r.getMessage() for r in caplog.records)


def test_without_a_database_nothing_is_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "database_url", "")
    journal = task_persistence.TaskJournal()
    journal.task_saved(_task(8), TOKEN)
    journal.trace_appended(_id(8), 0, TraceLine(t="00.000", level="exec", msg="x"))
    journal.plan_saved(_plan())
    assert journal.pending_writes == 0
    assert asyncio.run(journal.flush(0.1)) is True


# ── reading: a task this process never ran ──────────────────────────────
@pytest.fixture()
def client_on(store: FakeStore, client: Any) -> Any:
    return client


def test_a_task_from_before_the_restart_is_served_with_its_trace_and_artifact(store: FakeStore, client_on: Any) -> None:
    artifact = {"title": "Page", "files": [{"path": "index.html", "content": "<h1>¡hola!</h1>"}]}
    store.seed(_task(10, artifact=artifact, charge_tx="ab" * 32, settlement="settled"), lines=("one", "two"))

    task = client_on.get(f"/api/tasks/{_id(10)}")
    trace = client_on.get(f"/api/trace/{_id(10)}")
    art = client_on.get(f"/api/tasks/{_id(10)}/artifact")

    assert task.status_code == 200
    assert (task.json()["status"], task.json()["charge_tx"], task.json()["settlement"]) == (
        "complete",
        "ab" * 32,
        "settled",
    )
    assert [line["msg"] for line in trace.json()] == ["one", "two"]
    assert art.json()["artifact"] == artifact


def test_under_enforcement_a_restored_task_admits_exactly_who_it_did_before(
    store: FakeStore, client_on: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token is checked against the digest the store kept: the right token,
    in the header or the query, and the operator key read it; a wrong token or
    none at all is the same 404 as a task that does not exist."""
    monkeypatch.setattr(settings, "task_auth_required", True)
    monkeypatch.setattr(settings, "api_key", API_KEY)
    store.seed(_task(11), lines=("one",))
    url = f"/api/tasks/{_id(11)}"

    assert client_on.get(url, headers={"X-Task-Token": TOKEN}).status_code == 200
    assert client_on.get(f"/api/trace/{_id(11)}?token={TOKEN}").status_code == 200
    assert client_on.get(url, headers={"X-API-Key": API_KEY}).status_code == 200
    for refused in (
        client_on.get(url),
        client_on.get(url, headers={"X-Task-Token": TOKEN + "x"}),
        client_on.get(url, headers={"X-Task-Token": read_token_digest(TOKEN)}),  # the digest is not the token
        client_on.get(f"/api/tasks/{_id(99)}", headers={"X-Task-Token": TOKEN}),
    ):
        assert (refused.status_code, refused.json()["error"]["code"]) == (404, "unknown_task")
    assert _id(11) not in state.task_tokens  # the token itself is never held for it


def test_a_store_that_cannot_answer_is_a_prompt_503_not_a_404(
    store: FakeStore, client_on: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_persistence, "READ_TIMEOUT_SECONDS", 0.05)
    store.read_delay = 30.0
    began = time.monotonic()
    slow = client_on.get(f"/api/tasks/{_id(12)}")
    store.read_delay, store.down = 0.0, True
    down = client_on.get(f"/api/trace/{_id(12)}")

    assert time.monotonic() - began < 5.0
    for r in (slow, down):
        assert (r.status_code, r.json()["error"]["code"]) == (503, "task_store_unavailable")
        assert r.headers["Retry-After"] == "5"


def test_a_miss_is_remembered_and_an_id_never_minted_is_never_looked_up(store: FakeStore, client_on: Any) -> None:
    for _ in range(3):
        assert client_on.get(f"/api/tasks/{_id(13)}").status_code == 404
    assert client_on.get("/api/tasks/tsk_not-an-id").status_code == 404
    assert client_on.get("/api/tasks/" + "tsk_" + "a" * 500).status_code == 404
    assert store.loads == [_id(13)]


def test_a_restored_task_does_not_jump_the_task_list(store: FakeStore) -> None:
    s = AppState()
    for n in (20, 21):
        s.add_task(_task(n))
    old = _task(22).model_copy(update={"started_at": 1.0})
    s.hydrate_task(old, read_token_digest(TOKEN), [])
    assert [t.id for t in s.recent_tasks()] == [_id(21), _id(20), _id(22)]


def test_a_run_another_process_abandoned_is_closed_as_interrupted(store: FakeStore, client_on: Any) -> None:
    """Left `running` by a process that is gone, with no write for longer than
    any run stays silent: read back as failed, with the reason in its trace,
    and that correction written back so every later reader agrees."""
    store.seed(_task(30, "running"), lines=("step 1",), age=task_persistence.ORPHAN_AFTER_SECONDS + 60)

    body = client_on.get(f"/api/tasks/{_id(30)}").json()
    trace = [line["msg"] for line in client_on.get(f"/api/trace/{_id(30)}").json()]
    asyncio.run(task_persistence.flush(1.0))

    assert body["status"] == "failed"
    assert trace == ["step 1", task_persistence.INTERRUPTED_MESSAGE]
    assert store.tasks[_id(30)].status == "failed"
    assert store.traces[(_id(30), 1)]


def test_a_run_another_process_is_still_finishing_is_re_read_not_frozen(
    store: FakeStore, client_on: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deploy overlaps two processes: the old one finishes a run the new one
    is already asked about. The new one re-reads it instead of serving its
    first `running` forever — and never declares it dead."""
    monkeypatch.setattr(task_persistence, "FOREIGN_REFRESH_SECONDS", 0.0)
    store.seed(_task(31, "running"), lines=("step 1",))

    first = client_on.get(f"/api/tasks/{_id(31)}").json()["status"]
    store.seed(_task(31, "complete"), lines=("step 1", "sealed"))
    second = client_on.get(f"/api/tasks/{_id(31)}").json()["status"]

    assert (first, second) == ("running", "complete")
    assert [line["msg"] for line in client_on.get(f"/api/trace/{_id(31)}").json()] == ["step 1", "sealed"]


# ── reading: a plan authorised before the restart ───────────────────────
def test_a_plan_from_before_the_restart_still_executes(
    store: FakeStore, client_on: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)

    class _Ok:
        name = "w.ok"

        async def run(self, intent: str, rationale: str, context: Any = None) -> dict[str, Any]:
            return {"summary": "done"}

    async def _resolve(agent_id: str) -> _Ok:
        return _Ok()

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)
    store.plans["pln_0a0b0c0d"] = _plan()

    r = client_on.post("/api/orchestrator/execute", json={"plan_id": "pln_0a0b0c0d"})

    assert r.status_code == 200, r.text
    assert "pln_0a0b0c0d" in state.plans


def test_a_plan_store_that_cannot_answer_is_a_503_and_releases_nothing(
    store: FakeStore, client_on: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the paid path an UNKNOWN plan releases the buyer's custody. A plan
    that merely could not be read must not."""
    from app.services import authorization_guard

    released: list[Any] = []

    async def _release(*a: Any, **k: Any) -> None:
        released.append(a)

    monkeypatch.setattr(authorization_guard, "release_if_owned", _release)
    store.down = True

    r = client_on.post(
        "/api/orchestrator/execute",
        json={"plan_id": "pln_0a0b0c0d", "auth_id_hex": "ab" * 16, "payer": "G" + "A" * 55},
    )

    assert (r.status_code, r.json()["error"]["code"]) == (503, "plan_store_unavailable")
    assert released == []
