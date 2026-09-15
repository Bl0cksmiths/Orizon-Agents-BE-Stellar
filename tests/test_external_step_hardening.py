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

from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.state import state
from app.stellar import client as sc

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


# ── the settler rates what the step actually delivered ─────────────

AUTH = "ab" * 16
PAYER = "G" + "A" * 55
JOB_ID = b"\x01" * 16


def _settles(monkeypatch) -> list[tuple[str, int]]:
    """Put the run on the on-chain path with the money stubbed out, and record
    every (agent_id, rating) the settler submits.

    Only the two calls that leave the process are replaced — the charge/seal
    and the rating submit. `_submit_ratings` itself, and the lookup that feeds
    it, are the code under test.
    """
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_signing_key", "SFAKEKEY")

    async def _fake_settle(task_id, start, plan, *, payer, auth_id_hex, total_usdc):
        return ("0x" + "c" * 8, "0x" + "p" * 8, JOB_ID)

    monkeypatch.setattr(execution_svc, "_settle_onchain", _fake_settle)

    calls: list[tuple[str, int]] = []

    async def _fake_submit(agent_id, job_id, rating, weight, payer, kind="auto"):
        calls.append((agent_id, rating))
        return {"hash": "deadbeefcafe0123", "status": "SUCCESS"}

    monkeypatch.setattr(sc, "submit_rating_async", _fake_submit)
    return calls


def test_a_delivered_external_step_is_not_rated_twenty(monkeypatch):
    """The lookup used to read the CATALOG name while the output was written
    under the WORKER name — which for an external agent is "external.<id>",
    never the operator's on-chain Agent.name. It missed every time, and
    `synthetic_rating(None, …)` wrote a permanent 20/100 ("settled money for
    no delivered work") for an operator who delivered."""
    calls = _settles(monkeypatch)
    _resolves_to(
        monkeypatch,
        {
            "ext_rated": _Worker("external.ext_rated", GOOD_OUTPUT),
            "agt_good": _Worker("w.good", GOOD_OUTPUT),
        },
    )
    task_id = "tsk_hostile_rated"
    _add_task(task_id, 2)
    asyncio.run(
        execution_svc._run(
            _plan("pln_rated", ("ext_rated", "Acme Renderer"), ("agt_good", "w.good")),
            task_id,
            auth_id_hex=AUTH,
            payer=PAYER,
        )
    )

    # Both steps shipped the same clean artifact, so both earn the same score:
    # the external one is not penalised for the shape of its worker name.
    assert calls == [("ext_rated", 95), ("agt_good", 95)]
    lines = state.traces[task_id]
    assert any(ln.level == "proof" and "Acme Renderer rated 95/100" in ln.msg for ln in lines)


class _RaisingWorker:
    """Dispatched, and failed — the operator was asked and did not deliver."""

    real = True

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, intent, rationale, context=None):
        raise RuntimeError("operator endpoint blew up")


def test_a_dispatched_step_that_delivered_nothing_is_still_rated_twenty(monkeypatch):
    """The other half of the re-key: fixing the miss must not hand a score to
    a step that produced no output at all.

    The failing agent here is DISPATCHED and raises. That distinction is the
    point — see the companion test below. Using an unresolvable agent to stand
    for a failed one conflates "did not deliver" with "was never asked".
    """
    calls = _settles(monkeypatch)
    _resolves_to(
        monkeypatch,
        {"ext_broken": _RaisingWorker("external.ext_broken"), "agt_good": _Worker("w.good", GOOD_OUTPUT)},
    )
    task_id = "tsk_hostile_unrated"
    _add_task(task_id, 2)
    asyncio.run(
        execution_svc._run(
            _plan("pln_unrated", ("ext_broken", "Broken"), ("agt_good", "w.good")),
            task_id,
            auth_id_hex=AUTH,
            payer=PAYER,
        )
    )

    assert calls == [("ext_broken", 20), ("agt_good", 95)]


def test_a_step_that_was_never_dispatched_is_not_rated(monkeypatch):
    """resolve_worker fails OPEN, so an unreadable binding store returns None
    exactly like a missing binding — and the failure is negative-cached, so one
    blip can hit several steps. Rating here would write a permanent on-chain
    20/100 against an operator we never asked to deliver (ADR 0005 D5)."""
    calls = _settles(monkeypatch)
    _resolves_to(monkeypatch, {"agt_good": _Worker("w.good", GOOD_OUTPUT)})
    task_id = "tsk_never_dispatched"
    _add_task(task_id, 2)
    asyncio.run(
        execution_svc._run(
            _plan("pln_undispatched", ("ext_absent", "Ghost"), ("agt_good", "w.good")),
            task_id,
            auth_id_hex=AUTH,
            payer=PAYER,
        )
    )

    assert calls == [("agt_good", 95)]
    assert not any(agent == "ext_absent" for agent, _ in calls)


def test_operator_supplied_source_cannot_buy_the_baked_rating(monkeypatch):
    """`source` is an operator-settable field of the published envelope and
    `synthetic_rating` pays a flat 95 for `source="baked"`. With the lookup
    fixed that branch became reachable self-dealing: a one-line response for
    a 95/100 on-chain. The untrusted worker's `source` never reaches it."""
    calls = _settles(monkeypatch)
    _resolves_to(monkeypatch, {"ext_baked": _Worker("external.ext_baked", {"summary": "ok", "source": "baked"})})
    task_id = "tsk_hostile_baked"
    _add_task(task_id, 1)
    asyncio.run(execution_svc._run(_plan("pln_baked", ("ext_baked", "Acme")), task_id, auth_id_hex=AUTH, payer=PAYER))

    # 70: output was delivered, but nothing in it is checkable evidence — no
    # artifact and no critic verdict. Emphatically not the baked 95.
    # 20, not 70: stripping `source` denies the 95, and ADR 0005 D3 then denies
    # the base too, because this response carries nothing checkable. Answering
    # with a bare acknowledgement now scores exactly like a dead endpoint —
    # before, it scored the prior and RAISED the agent's lower bound.
    assert calls == [("ext_baked", 20)]


def test_a_first_party_worker_still_earns_the_baked_rating(monkeypatch):
    """Baked kit artifacts are repo-owned, deterministic and pre-validated —
    the 95 is theirs. Withholding `source` from untrusted workers must not
    quietly re-score the local kit path."""
    calls = _settles(monkeypatch)
    worker = _Worker("code.gen", {"summary": "ok", "source": "baked"})
    _resolves_to(monkeypatch, {"agt_11c0": worker})
    monkeypatch.setattr(execution_svc, "get_worker", lambda agent_id: worker if agent_id == "agt_11c0" else None)
    task_id = "tsk_hostile_first_party"
    _add_task(task_id, 1)
    asyncio.run(
        execution_svc._run(_plan("pln_first_party", ("agt_11c0", "code.gen")), task_id, auth_id_hex=AUTH, payer=PAYER)
    )

    assert calls == [("agt_11c0", 95)]


# ── operator text in the buyer's trace is bounded and scheme-checked ─


def _run_one(monkeypatch, task_id: str, output: Any) -> list:
    """Run a one-step plan against `output` and hand back its trace lines."""
    _resolves_to(monkeypatch, {"ext_trace": _Worker("external.ext_trace", output)})
    _add_task(task_id, 1)
    asyncio.run(execution_svc._run(_plan("pln_" + task_id, ("ext_trace", "Acme")), task_id))
    return state.traces[task_id]


def test_an_over_long_artifact_title_is_capped_in_the_trace(monkeypatch):
    """Trace lines are retained with the task and rendered in the buyer's
    viewer, and every other traced value is already held to 180 chars — a
    title an operator chose does not get to be the exception."""
    title = "T" * 500
    lines = _run_one(
        monkeypatch,
        "tsk_hostile_long_title",
        {"summary": "ok", "artifact": {"title": title, "files": [{"content": "x"}]}},
    )

    traced = next(ln for ln in lines if ln.level == "artifact")
    assert "T" * 180 in traced.msg
    assert "T" * 181 not in traced.msg
    # Only the trace is capped: the artifact is still handed back whole.
    artifact = state.tasks["tsk_hostile_long_title"].artifact
    assert artifact is not None and artifact["title"] == title


@pytest.mark.parametrize(
    ("label", "url"),
    [
        ("javascript", "javascript:alert(document.cookie)"),
        ("data", "data:text/html,<script>steal()</script>"),
        ("scheme-relative", "//wallet-drainer.example/connect"),
        ("ftp", "ftp://wallet-drainer.example/x"),
        ("not-a-string", {"href": "https://ok.example"}),
    ],
)
def test_a_non_http_preview_url_never_reaches_the_trace(monkeypatch, label, url):
    """`preview_url` is rendered as the run's "ship" moment, so an operator
    could otherwise put a `javascript:` or phishing link in front of the buyer
    under the orchestrator's own voice."""
    lines = _run_one(monkeypatch, f"tsk_hostile_url_{label.replace('-', '_')}", {"summary": "ok", "preview_url": url})

    assert not any("preview →" in ln.msg for ln in lines)
    # Refusing the link is not a step failure — the step still delivered.
    assert state.tasks[f"tsk_hostile_url_{label.replace('-', '_')}"].status == "complete"


def test_an_http_preview_url_is_still_surfaced_but_capped(monkeypatch):
    """The legitimate deploy.v0 case keeps working, under the same ceiling."""
    url = "https://operator.example/preview/" + "p" * 400
    lines = _run_one(monkeypatch, "tsk_hostile_url_long", {"summary": "ok", "preview_url": url})

    traced = next(ln for ln in lines if "preview →" in ln.msg)
    assert url[:180] in traced.msg
    assert url not in traced.msg
