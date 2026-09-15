"""Story 2.01 at the DISPATCH seam: a binding that exists has to actually run.

The bind route made a binding durable; until `execution_svc._run` resolved one,
a bound operator endpoint was still skipped as an "unknown agent" — a binding
that survived a restart and did nothing. These tests drive the real run loop
over the real `binding_registry.resolve_worker` and the real
`ExternalHttpWorker`, injecting only the two things that would otherwise reach
outside the process: the binding store, and the HTTP transport underneath the
worker's own client. Nothing here opens a socket.

The three cases are the whole contract of the seam:

  * a bound agent is dispatched to the endpoint the STORE holds;
  * an agent with neither a local worker nor a binding is skipped exactly as it
    was before the seam existed;
  * a store that cannot be read loses the step, never the run — resolution
    fails open, because ownership was already proved at bind time and a read
    failure is a degraded workflow, not an escalation.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.agents.workers import external_http
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import binding_registry, execution_svc
from app.services.binding_store import InMemoryBindingStore
from app.state import state
from app.stellar import cache as rcache

INTENT = "build a bakery landing page"
# A public https URL: the endpoint is validated before every dispatch (SSRF),
# and the MockTransport below answers it in-process, so this is only a label.
ENDPOINT = "https://operator.example/run"
OWNER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
PRICE = 0.02


@pytest.fixture(autouse=True)
def clean_seam():
    """resolve_worker reads the store through the shared TTL cache, which also
    negative-caches failures — so one test's binding, or one test's unreadable
    store, must not still be answering in the next one."""
    rcache.clear()
    yield
    rcache.clear()
    for task_id in [t for t in state.tasks if t.startswith("tsk_bound_")]:
        state.tasks.pop(task_id, None)
    for task_id in [t for t in state.traces if t.startswith("tsk_bound_")]:
        state.traces.pop(task_id, None)


def _plan(plan_id: str, *agent_ids: str) -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent=INTENT,
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id=agent_id,
                    agent_name=f"w.{agent_id}",
                    rationale="build the page",
                    est_price_usdc=PRICE,
                    est_eta_seconds=1.0,
                )
                for agent_id in agent_ids
            ]
        ),
        total_usdc=PRICE * len(agent_ids),
        total_eta=1.0,
    )


def _add_task(task_id: str, steps: int = 1) -> None:
    state.add_task(Task(id=task_id, intent=INTENT, agents=steps, spent=0.0, status="running"))


def _store_holding(monkeypatch, *bindings: tuple[str, str]) -> InMemoryBindingStore:
    """Inject a binding store at the resolver's seam, pre-loaded via its own
    async interface — no test-only hooks into how bindings are stored."""
    store = InMemoryBindingStore()
    for agent_id, endpoint in bindings:
        asyncio.run(store.put(agent_id, endpoint, OWNER))
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: store)
    return store


def _intercept_dispatch(monkeypatch, handler=None) -> list[httpx.Request]:
    """Answer every dispatch in-process and record what was sent.

    `resolve_worker` builds the ExternalHttpWorker itself, so there is no
    client to inject by the time the run loop has one: the transport is swapped
    under httpx.AsyncClient instead, which also keeps the per-dispatch client
    (and its timeouts and follow_redirects=False) exactly as production builds
    it. An empty returned list is therefore proof no dispatch was attempted.
    """
    seen: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if handler is None:
            raise AssertionError(f"unexpected dispatch to {request.url}")
        return handler(request)

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_record)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(external_http.httpx, "AsyncClient", _factory)
    return seen


def test_bound_external_agent_is_dispatched_to_its_endpoint(monkeypatch):
    """The point of the story: an agent id with no local worker but a stored
    binding is executed against that binding's endpoint, and its response is
    billed and kept like any other step's."""
    _store_holding(monkeypatch, ("ext_bound1", ENDPOINT))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "summary": "external agent built the page",
                "artifact": {"title": "landing.html", "files": [{"content": "<!doctype html>\n"}]},
            },
        )

    seen = _intercept_dispatch(monkeypatch, handler)
    task_id = "tsk_bound_dispatch"
    _add_task(task_id)

    asyncio.run(execution_svc._run(_plan("pln_bound_dispatch", "ext_bound1"), task_id))

    # Dispatched exactly once, to the endpoint the STORE holds — not a default,
    # not a cached one from another agent.
    assert [str(r.url) for r in seen] == [ENDPOINT]
    body = json.loads(seen[0].read())
    assert body["agent_id"] == "ext_bound1"
    assert body["intent"] == INTENT
    assert body["rationale"] == "build the page"

    task = state.tasks[task_id]
    assert task.status == "complete"
    assert task.spent == PRICE  # the external step was billed at its plan price
    assert task.artifact is not None
    assert task.artifact["title"] == "landing.html"
    # Named as a bound external agent, so the run went through the binding
    # path rather than finding something local under that id.
    lines = state.traces[task_id]
    assert any("match agent: external.ext_bound1 (ext_bound1)" in ln.msg for ln in lines)
    assert any(ln.level == "out" and "external agent built the page" in ln.msg for ln in lines)
    assert not any(ln.level == "error" for ln in lines)
