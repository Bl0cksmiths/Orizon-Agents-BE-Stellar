from __future__ import annotations

import hashlib
import time
from collections import deque
from typing import Protocol

from .schemas import Agent, StoredPlan, Task, TraceLine

# Task statuses eligible for first-choice eviction (see add_task).
_TERMINAL = frozenset({"complete", "failed"})


def read_token_digest(token: str) -> str:
    """The SHA-256 hex digest a task read token is kept under outside this process.

    The durable task store never holds the token itself (D-090): a row read
    out of the database must not be a working credential. Encoded exactly as
    `task_auth._matches` encodes a candidate — utf-8, unencodable characters
    dropped — so a token proves the same task whether it is checked against
    the plaintext minted here or against the digest a restart reloaded.
    """
    return hashlib.sha256(token.encode("utf-8", "ignore")).hexdigest()


class StateObserver(Protocol):
    """What the durable task store is told about every write to `AppState`.

    Called synchronously, from inside the mutation, so an implementation must
    only RECORD the change and return — the write itself happens elsewhere
    (`services/task_persistence.py`, write-behind). A hydration from the store
    is never reported back to it.
    """

    def task_saved(self, task: Task, read_token: str | None) -> None: ...

    def trace_appended(self, task_id: str, seq: int, line: TraceLine) -> None: ...

    def plan_saved(self, plan: StoredPlan) -> None: ...


# Module-level rather than an AppState attribute: the restart tests reset the
# one shared `state` with `state.__init__()`, which must wipe what the process
# held in memory and NOT unplug the durable store from it.
_observer: StateObserver | None = None


def set_observer(observer: StateObserver | None) -> None:
    """Install (or, with None, remove) the observer every AppState reports to."""
    global _observer
    _observer = observer


class AppState:
    """Process-local hot cache of agents, tasks, plans, and traces.

    Single-worker by design: render.yaml pins uvicorn to --workers 1, so all
    in-memory state lives in this one process. Tasks, their traces and read
    tokens, and stored plans are ALSO written through to Postgres when
    DATABASE_URL is set (services/task_persistence.py, D-090): every write
    below reports itself to the installed `StateObserver`, and a task or plan
    this process does not hold is read back from the store on demand
    (`hydrate_task`, `hydrate_plan`). Without DATABASE_URL these structures
    are the only copy and are lost on restart.

    Retention is bounded: only the newest `task_order.maxlen` tasks (with
    their traces) and `plan_order.maxlen` plans are kept — older entries are
    evicted on insert so a long-lived process can't grow without bound. An
    evicted task is not lost when the durable store is on: the next read of
    it hydrates it again.
    """

    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.tasks: dict[str, Task] = {}
        # Per-task capability read tokens, minted at execute. Kept OFF the
        # Task model (it is a response shape — a token field would leak) and
        # evicted in lockstep with tasks/traces below.
        self.task_tokens: dict[str, str] = {}
        # The digest of the read token of every task hydrated from the
        # durable store, which keeps digests only (`read_token_digest`).
        # Evicted in lockstep with `task_tokens`.
        self.task_token_digests: dict[str, str] = {}
        self.task_order: deque[str] = deque(maxlen=200)
        self.traces: dict[str, list[TraceLine]] = {}
        self.plans: dict[str, StoredPlan] = {}
        self.plan_order: deque[str] = deque(maxlen=200)
        self.started_at: float = time.time()

    def add_agent(self, agent: Agent) -> None:
        self.agents[agent.id] = agent

    def list_agents(self) -> list[Agent]:
        return list(self.agents.values())

    def add_task(self, task: Task, *, read_token: str | None = None) -> None:
        """Hold a NEW task (with its read token, when one was minted) as the newest."""
        self._make_room(task.id)
        self.tasks[task.id] = task
        self.task_order.appendleft(task.id)
        if read_token is not None:
            self.task_tokens[task.id] = read_token
        if _observer is not None:
            _observer.task_saved(task, read_token)

    def put_task(self, task: Task) -> None:
        """Replace a held task with its next state (status, settlement, artifact…).

        A task this process no longer holds — evicted, or never here — is not
        brought back by an update: the store already has its last state, and
        resurrecting it here would let a late write grow the cache past its cap.
        """
        if task.id not in self.tasks:
            return
        self.tasks[task.id] = task
        if _observer is not None:
            _observer.task_saved(task, None)

    def hydrate_task(self, task: Task, read_token_digest: str | None, traces: list[TraceLine]) -> None:
        """Hold a task read back from the durable store; not reported back to it.

        Placed in `task_order` by `started_at` rather than as the newest, so a
        week-old receipt someone opened does not jump to the top of the task
        list or into the newest-first window the metrics read.
        """
        if task.id in self.tasks:
            return
        self._make_room(task.id)
        index = next(
            (
                i
                for i, tid in enumerate(self.task_order)
                if (t := self.tasks.get(tid)) and t.started_at < task.started_at
            ),
            len(self.task_order),
        )
        self.task_order.insert(index, task.id)
        self.tasks[task.id] = task
        self.traces[task.id] = list(traces)
        if read_token_digest is not None:
            self.task_token_digests[task.id] = read_token_digest

    def _make_room(self, task_id: str) -> None:
        # At capacity, evict the oldest TERMINAL (complete/failed) task first —
        # dropping by age alone could evict a still-running workflow, 404ing
        # its SSE stream and no-oping its finalize. Fall back to the oldest
        # overall so the store can never exceed its cap (mirrors
        # app/pdax/ramp_store.py). task_order is newest-first, so the oldest
        # entries sit at the right end.
        if len(self.task_order) == self.task_order.maxlen and task_id not in self.tasks:
            evicted = next(
                (tid for tid in reversed(self.task_order) if (t := self.tasks.get(tid)) and t.status in _TERMINAL),
                self.task_order[-1],
            )
            self.task_order.remove(evicted)
            self.tasks.pop(evicted, None)
            self.traces.pop(evicted, None)
            self.task_tokens.pop(evicted, None)
            self.task_token_digests.pop(evicted, None)

    def add_plan(self, plan: StoredPlan) -> None:
        self._hold_plan(plan)
        if _observer is not None:
            _observer.plan_saved(plan)

    def hydrate_plan(self, plan: StoredPlan) -> None:
        """Hold a plan read back from the durable store; not reported back to it."""
        if plan.id not in self.plans:
            self._hold_plan(plan)

    def _hold_plan(self, plan: StoredPlan) -> None:
        evicted = self.plan_order[-1] if len(self.plan_order) == self.plan_order.maxlen else None
        self.plans[plan.id] = plan
        self.plan_order.appendleft(plan.id)
        if evicted is not None and evicted != plan.id:
            self.plans.pop(evicted, None)

    def recent_tasks(self, limit: int = 20) -> list[Task]:
        return [self.tasks[tid] for tid in list(self.task_order)[:limit] if tid in self.tasks]

    def append_trace(self, task_id: str, line: TraceLine) -> None:
        lines = self.traces.setdefault(task_id, [])
        lines.append(line)
        if _observer is not None:
            # The line's index is its key in the store, so a retried write of
            # the same line lands on the same row instead of a second one.
            _observer.trace_appended(task_id, len(lines) - 1, line)


state = AppState()
