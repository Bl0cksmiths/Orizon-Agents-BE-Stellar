"""A bound external agent's output is untrusted input — the run must survive it.

Story 2.01 put an operator's JSON on the inside of `execution_svc._run`: the
`output` a step returns is now whatever a third party chose to send. The
post-step handling that reads it (critic lists, `artifact`, `artifact.files`)
sits OUTSIDE the per-step try/except, so a single wrong type used to escape to
the run-level handler — the task finalized "failed", `_settle_onchain` never
ran, and every honest agent in the plan went unpaid for a run they completed.

These tests drive the real run loop with the dispatch seam pinned to a worker
that answers with output we chose, which is exactly the shape a hostile (or
merely sloppy) operator endpoint can produce. The contract they pin:

  * a bad shape degrades that STEP — unbilled, skipped — never the run;
  * a step that delivered is rated on what it delivered, not 20/100 because
    the settler looked it up under the wrong key;
  * an operator cannot grade itself, and cannot push unbounded or
    non-http(s) text into the buyer's trace.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.state import state

INTENT = "render the launch deck"
PRICE = 0.02


@pytest.fixture(autouse=True)
def clean_tasks():
    """Tasks and traces live in process-wide state; keep each case's leftovers
    out of the next one's assertions."""
    yield
    for task_id in [t for t in state.tasks if t.startswith("tsk_hostile_")]:
        state.tasks.pop(task_id, None)
    for task_id in [t for t in state.traces if t.startswith("tsk_hostile_")]:
        state.traces.pop(task_id, None)


class _Worker:
    """Whatever the dispatch seam resolved, answering with a fixed output.

    Stands in for a bound operator endpoint: `ExternalHttpWorker` guarantees
    only that the response was a JSON object carrying a non-empty `summary`,
    so every other field arrives shaped however the operator sent it.
    """

    real = True

    def __init__(self, name: str, output: Any) -> None:
        self.name = name
        self._output = output

    async def run(self, intent: str, rationale: str, context: dict | None = None) -> Any:
        return self._output


def _resolves_to(monkeypatch, workers: dict[str, _Worker]) -> None:
    """Pin the dispatch seam to `workers`, by agent_id.

    `resolve_worker` is the one seam between the run loop and the outside
    world (local registry, then the binding store, then HTTP). Replacing it
    keeps every line of the run loop real while the output under test is ours.
    """

    async def _resolve(agent_id: str) -> _Worker | None:
        return workers.get(agent_id)

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)


def _plan(plan_id: str, *steps: tuple[str, str]) -> StoredPlan:
    """A plan over (agent_id, agent_name) pairs.

    They are spelled separately on purpose: for a bound external agent the
    catalog `agent_name` is the operator's on-chain `Agent.name` and the
    worker runs as "external.<agent_id>", so anything that assumes the two
    match is exactly what these tests are here to catch.
    """
    return StoredPlan(
        id=plan_id,
        intent=INTENT,
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    rationale="render it",
                    est_price_usdc=PRICE,
                    est_eta_seconds=1.0,
                )
                for agent_id, agent_name in steps
            ]
        ),
        total_usdc=PRICE * len(steps),
        total_eta=1.0,
    )


def _add_task(task_id: str, steps: int) -> None:
    state.add_task(Task(id=task_id, intent=INTENT, agents=steps, spent=0.0, status="running"))


GOOD_OUTPUT: dict[str, Any] = {
    "summary": "built the page",
    "artifact": {"title": "landing.html", "files": [{"path": "index.html", "content": "<p>hi</p>\n"}]},
    "critic_violations": [],
}


def _run_with_hostile_step(monkeypatch, task_id: str, hostile: Any) -> Task:
    """Run a two-step plan whose FIRST step returns `hostile` and whose second
    delivers normally, and hand back the terminal task."""
    _resolves_to(
        monkeypatch,
        {
            "ext_hostile": _Worker("external.ext_hostile", hostile),
            "agt_good": _Worker("w.good", GOOD_OUTPUT),
        },
    )
    _add_task(task_id, 2)
    asyncio.run(execution_svc._run(_plan("pln_" + task_id, ("ext_hostile", "Acme"), ("agt_good", "w.good")), task_id))
    return state.tasks[task_id]


# ── a hostile shape degrades the step, never the run ───────────────


def test_string_artifact_degrades_the_step_not_the_run(monkeypatch, caplog):
    """The one-line response that used to destroy a paid workflow: `artifact`
    is a string, so `art.get("title")` raised AttributeError outside the
    per-step try, the run finalized "failed", and the settlement that pays
    every other agent never ran."""
    task_id = "tsk_hostile_artifact_str"
    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        task = _run_with_hostile_step(monkeypatch, task_id, {"summary": "ok", "artifact": "boom"})

    assert task.status != "failed"
    assert task.status == "complete"  # the honest step delivered, so the run did
    assert task.spent == PRICE  # only the step that delivered was billed
    assert task.artifact is not None and task.artifact["title"] == "landing.html"

    lines = state.traces[task_id]
    assert any(ln.level == "error" and "external.ext_hostile returned an unusable artifact" in ln.msg for ln in lines)
    # The run-level handler is what leaks raw exception text into the trace.
    assert not any("workflow failed" in ln.msg for ln in lines)
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.services.execution_svc"]
    assert any("ext_hostile" in m and "'artifact'" in m for m in msgs)


@pytest.mark.parametrize(
    ("label", "hostile"),
    [
        ("artifact-as-list", {"summary": "ok", "artifact": ["boom"]}),
        ("files-as-string", {"summary": "ok", "artifact": {"title": "t", "files": "index.html"}}),
        ("files-of-non-dicts", {"summary": "ok", "artifact": {"title": "t", "files": ["index.html", 7]}}),
        ("file-content-not-a-string", {"summary": "ok", "artifact": {"title": "t", "files": [{"content": 7}]}}),
        ("violations-of-non-strings", {"summary": "ok", "critic_violations": [1, 2]}),
        ("violations-as-dict", {"summary": "ok", "critic_violations": {"missing": "doctype"}}),
        ("notes-as-dict", {"summary": "ok", "critic_notes": {"note": "hi"}}),
    ],
)
def test_hostile_field_shapes_never_sink_the_run(monkeypatch, label, hostile):
    """Every field the post-step handling reads, wrong-typed. Each one raised
    (TypeError from the joins, AttributeError from the artifact walk) and each
    one must now cost its own step and nothing else."""
    task_id = f"tsk_hostile_{label.replace('-', '_')}"
    task = _run_with_hostile_step(monkeypatch, task_id, hostile)

    assert task.status == "complete", f"{label} sank the run"
    assert task.spent == PRICE, f"{label} was billed despite being unusable"
    assert task.artifact is not None and task.artifact["title"] == "landing.html"
    lines = state.traces[task_id]
    assert any(ln.level == "error" and "unusable" in ln.msg for ln in lines)


def test_a_degraded_step_leaves_no_output_for_later_steps(monkeypatch):
    """A refused step is a step that produced nothing: it must not seed the
    context a later worker builds on, or the bad shape simply moves."""
    seen: list[dict] = []

    class _Recorder(_Worker):
        async def run(self, intent, rationale, context=None):
            seen.append(dict(context or {}))
            return GOOD_OUTPUT

    _resolves_to(
        monkeypatch,
        {
            "ext_hostile": _Worker("external.ext_hostile", {"summary": "ok", "artifact": "boom"}),
            "agt_good": _Recorder("w.good", GOOD_OUTPUT),
        },
    )
    task_id = "tsk_hostile_context"
    _add_task(task_id, 2)
    asyncio.run(execution_svc._run(_plan("pln_ctx", ("ext_hostile", "Acme"), ("agt_good", "w.good")), task_id))

    assert seen, "the second step never ran"
    assert "external.ext_hostile" not in seen[0]
