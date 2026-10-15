"""Characterization of `_run`, the run loop, end to end.

Golden runs over a plan that hits every per-step outcome — delivered (with
critic lines, a preview and an artifact), refused at execute, unknown agent,
raised, not attempted, wrong shape, unusable field, timed out, and a bound
operator's delivery — pin the whole trace in order, the failure tracker's
calls, the task's terminal fields, and for a paid run what the settle, the
settlement record and the ratings are handed. Then the run-level handlers:
a failure or cancellation before and after the task is final.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from test_settle_v2 import AUTH_ID_HEX, PAYER, SETTLE_TX, _auth, _Chain, _install, _payouts, _plan, _Store

from app.config import settings
from app.schemas import PlanStage, Task
from app.services import dispute_store, execution_svc
from app.state import state

TASK = "tsk_mc_run"
AGENTS = (
    "agt_ok",
    "agt_refused",
    "agt_unknown",
    "agt_boom",
    "agt_paused",
    "agt_nodict",
    "agt_badfield",
    "agt_hang",
    "agt_ext",
)
ARTIFACT = {"title": "App", "files": [{"content": "a\nb"}, {"content": "c"}]}


class _Paused(RuntimeError):
    rule = "spend_cap_reached"


class _Worker:
    def __init__(self, name: str, answer: Any, *, sources: list[str] | None = None) -> None:
        self.name = name
        self.answer = answer
        self.sources = sources or []
        self.contexts: list[dict[str, Any]] = []

    def upstream_sources(self, context: dict[str, Any] | None) -> list[str]:
        return self.sources

    async def run(self, intent: str, rationale: str, context: dict[str, Any] | None = None) -> Any:
        self.contexts.append(dict(context or {}))
        if self.answer == "hang":
            await asyncio.sleep(60)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _workers() -> dict[str, _Worker]:
    return {
        "agt_ok": _Worker(
            "w.ok",
            {
                "summary": "drafted it",
                "critic_violations": ["v1", "v2"],
                "critic_notes": ["n1"],
                "preview_url": "https://preview.test/p",
                "artifact": ARTIFACT,
                "source": "baked",
            },
        ),
        "agt_boom": _Worker("w.boom", RuntimeError("provider exploded")),
        "agt_paused": _Worker("w.paused", _Paused("cap")),
        "agt_nodict": _Worker("w.nodict", "not a dict"),
        "agt_badfield": _Worker("w.badfield", {"critic_notes": "not a list"}),
        "agt_hang": _Worker("w.hang", "hang"),
        "agt_ext": _Worker("external.agt_ext", {"counts": {"rows": 3}, "source": "baked", "summary": "x\x07y"}),
    }


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    workers = _workers()
    first_party = {a: w for a, w in workers.items() if a in {"agt_ok", "agt_paused", "agt_boom", "agt_hang"}}
    calls: list[tuple[str, ...]] = []

    async def _resolve(agent_id: str) -> _Worker | None:
        return workers.get(agent_id)

    def _refusal(step: Any, info: Any) -> str | None:
        return "agt_refused was delisted" if step.agent_id == "agt_refused" else None

    async def _ratings(*args: Any, **kwargs: Any) -> None:
        calls.append(("ratings",))
        harness_state["ratings"] = (args, kwargs)

    async def _no_reps(agent_ids: list[str]) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)
    monkeypatch.setattr(execution_svc, "get_worker", lambda agent_id: first_party.get(agent_id))
    monkeypatch.setattr(execution_svc, "_execute_refusal", _refusal)
    monkeypatch.setattr(execution_svc, "_submit_ratings", _ratings)
    monkeypatch.setattr(execution_svc.reputation_svc, "fetch_reps", _no_reps)
    monkeypatch.setattr(execution_svc, "STEP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(execution_svc.failure_tracker, "record_failure", lambda a, r: calls.append(("fail", a, r)))
    monkeypatch.setattr(execution_svc.failure_tracker, "record_success", lambda a: calls.append(("ok", a)))
    harness_state: dict[str, Any] = {"calls": calls, "workers": workers}
    yield harness_state
    state.tasks.pop(TASK, None)
    state.traces.pop(TASK, None)


def _mixed_plan() -> Any:
    plan = _plan(tuple(0.01 for _ in AGENTS), AGENTS)
    return plan.model_copy(update={"stages": [PlanStage(stage="plan", msg="planner: six steps")]})


def _start(plan: Any, **kwargs: Any) -> None:
    state.add_task(Task(id=TASK, intent=plan.intent, agents=len(plan.plan.steps), spent=0.0, status="running"))
    asyncio.run(execution_svc._run(plan, TASK, **kwargs))


def _trace() -> list[tuple[str, str, str | None]]:
    return [(line.level, re.sub(r"\d+\.\d\ds$", "<t>s", line.msg), line.settlement) for line in state.traces[TASK]]


STEP_LINES = [
    ("exec", "match agent: w.ok (agt_ok) — r", None),
    ("cost", f"x402 payment → agt_ok :: {execution_svc._amount(100_000)} (simulated)", None),
    ("out", "w.ok: drafted it", None),
    ("exec", "w.ok: violations → v1 · v2", None),
    ("exec", "w.ok: n1", None),
    ("out", "w.ok: preview → https://preview.test/p", None),
    ("artifact", "▣ App — 2 file(s) · 3 lines · 4 bytes", None),
    ("error", "step refused: agt_refused was delisted — not dispatched, not charged", None),
    ("error", "unknown agent: agt_unknown", None),
    ("exec", "match agent: w.boom (agt_boom) — r", None),
    ("error", "w.boom failed (unclassified)", None),
    ("exec", "match agent: w.paused (agt_paused) — r", None),
    ("error", "w.paused: paused: daily AI budget reached — not charged, not rated", None),
    ("exec", "match agent: w.nodict (agt_nodict) — r", None),
    ("exec", "w.nodict receives output from: w.ok", None),
    ("error", "w.nodict returned an unusable result", None),
    ("exec", "match agent: w.badfield (agt_badfield) — r", None),
    ("exec", "w.badfield receives output from: w.ok", None),
    ("error", "w.badfield returned an unusable critic_notes", None),
    ("exec", "match agent: w.hang (agt_hang) — r", None),
    ("error", "w.hang timed out", None),
    ("exec", "match agent: external.agt_ext (agt_ext) — r", None),
    ("exec", "external.agt_ext receives output from: w.ok", None),
    ("cost", f"x402 payment → agt_ext :: {execution_svc._amount(100_000)} (simulated)", None),
    ("out", "external.agt_ext: x\x07y", None),
]

TRACKER = [
    ("ok", "agt_ok"),
    ("fail", "agt_boom", "unclassified"),
    ("fail", "agt_nodict", "not_a_dict"),
    ("fail", "agt_badfield", "unusable_output"),
    ("fail", "agt_hang", "step_timeout"),
    ("ok", "agt_ext"),
]


def test_a_simulated_run_traces_every_step_outcome_in_order(harness: dict[str, Any]) -> None:
    _start(_mixed_plan())

    head = [
        ("input", "intent received → 'build a thing'", None),
        ("exec", "planner: six steps", None),
        ("exec", f"orchestrator: decompose → [{', '.join(AGENTS)}]", None),
        (
            "exec",
            f"reputation re-check unavailable for [{', '.join(sorted(AGENTS))}] — "
            "those steps run on the scores this plan was authorised with",
            None,
        ),
    ]
    tail = [
        ("proof", "ERC-8004 attestation: <hash> (simulated)", None),
        ("proof", f"workflow sealed — 2 agents · {execution_svc._amount(200_000)} · <t>s", None),
    ]
    trace = _trace()
    assert re.fullmatch(r"ERC-8004 attestation: 0x[0-9a-f]{32} \(simulated\)", trace[-2][1])
    trace[-2] = ("proof", "ERC-8004 attestation: <hash> (simulated)", None)
    assert trace == head + STEP_LINES + tail
    assert harness["calls"] == TRACKER
    task = state.tasks[TASK]
    assert (task.status, task.spent_stroops, task.artifact, task.charge_tx, task.proof_tx) == (
        "complete",
        200_000,
        ARTIFACT,
        None,
        None,
    )
    # The bound operator reads the first-party step's output as it was; the
    # operator's own output is fenced on the way into `context`.
    ext_context = harness["workers"]["agt_ext"].contexts[0]
    assert ext_context["w.ok"]["summary"] == "drafted it"
    assert "ratings" not in harness


def test_a_paid_run_settles_records_and_rates_exactly_the_delivered_steps(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    store = _Store()
    monkeypatch.setattr(dispute_store, "_store", store)

    _start(_mixed_plan(), auth_id_hex=AUTH_ID_HEX, payer=PAYER)

    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": "agt_ok", "amount": 100_000}, {"agent_id": "agt_ext", "amount": 100_000}]
    trace = _trace()
    assert trace[3] == ("exec", f"x402 authorized on-chain by {PAYER[:4]}…{PAYER[-4:]} (auth {AUTH_ID_HEX[:8]}…)", None)
    assert [line for line in trace if "(simulated)" in line[1]] == []
    assert trace[-1][0] == "proof" and trace[-1][1].startswith("workflow sealed — 2 agents · ")
    record = store.recorded[0]
    assert [(s.step_index, s.delivered, s.output_summary) for s in record.steps] == [
        (0, True, "drafted it"),
        (1, False, None),
        (2, False, None),
        (3, False, None),
        (4, False, None),
        (5, False, None),
        (6, False, None),
        (7, False, None),
        (8, True, "x y"),
    ]
    args, kwargs = harness["ratings"]
    assert args[0] == TASK and args[2].id == "pln_v2"
    delivered = args[3]
    assert sorted(delivered) == [0, 8]
    assert delivered[0]["source"] == "baked" and "source" not in delivered[8]
    assert kwargs["payer"] == PAYER
    assert kwargs["job_id"] == bytes.fromhex(record.job_id_hex)
    assert kwargs["undispatched"] == frozenset({1, 2, 4})
    assert kwargs["first_party_ids"] == frozenset({"agt_ok"})
    task = state.tasks[TASK]
    assert (task.status, task.spent_stroops, task.charge_tx, task.settlement) == (
        "complete",
        200_000,
        SETTLE_TX,
        "settled",
    )
    assert harness["calls"][-1] == ("ratings",)


def test_a_run_where_nothing_delivered_is_failed_and_says_so(harness: dict[str, Any]) -> None:
    plan = _plan((0.01, 0.01), ("agt_boom", "agt_unknown"))

    _start(plan)

    assert _trace()[-1] == ("error", "workflow incomplete — 0/2 agents produced output", None)
    assert [line for line in _trace() if line[0] == "proof"] == []
    task = state.tasks[TASK]
    assert (task.status, task.spent_stroops, task.artifact) == ("failed", 0, None)


def test_a_paid_v1_run_where_nothing_delivered_skips_the_charge_and_still_rates(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()), version=1)

    _start(_plan((0.01,), ("agt_boom",)), auth_id_hex=AUTH_ID_HEX, payer=PAYER)

    assert chain.calls == []
    assert ("exec", "no agent produced output — skipping on-chain charge/seal", "skipped") in _trace()
    _, kwargs = harness["ratings"]
    assert kwargs["job_id"] == execution_svc.unsettled_job_id(TASK)
    assert state.tasks[TASK].status == "failed"


def test_a_paid_v2_run_where_nothing_delivered_releases_and_still_rates(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))

    _start(_plan((0.01,), ("agt_boom",)), auth_id_hex=AUTH_ID_HEX, payer=PAYER)

    [release] = chain.named("settle")
    assert _payouts(release) == []
    _, kwargs = harness["ratings"]
    assert kwargs["job_id"] == execution_svc.unsettled_job_id(TASK)
    task = state.tasks[TASK]
    assert (task.status, task.settlement, task.charge_tx, task.spent_stroops) == (
        "failed",
        "released",
        SETTLE_TX,
        0,
    )


def test_a_paid_run_that_did_not_settle_rates_under_the_task_job_id(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    _install(monkeypatch, _Chain(auth=_auth(), settle={"hash": SETTLE_TX, "status": "timeout"}))

    _start(_plan((0.01,), ("agt_ok",)), auth_id_hex=AUTH_ID_HEX, payer=PAYER)

    _, kwargs = harness["ratings"]
    assert kwargs["job_id"] == execution_svc.unsettled_job_id(TASK)
    task = state.tasks[TASK]
    assert (task.status, task.charge_tx, task.spent_stroops) == ("complete", None, 0)


# ── the run-level handlers ──────────────────────────────────────────────
def test_a_failure_before_the_settle_fails_the_task_and_releases_the_custody(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))

    async def _broken(agent_id: str) -> Any:
        raise RuntimeError("binding store bug")

    monkeypatch.setattr(execution_svc, "resolve_worker", _broken)

    _start(_plan((0.01,), ("agt_ok",)), auth_id_hex=AUTH_ID_HEX, payer=PAYER)

    [release] = chain.named("settle")
    assert _payouts(release) == []
    trace = _trace()
    assert trace[-2:] == [
        ("error", "workflow failed: binding store bug", None),
        ("cost", f"custody released to the buyer · tx {SETTLE_TX[:10]}…", "released"),
    ]
    task = state.tasks[TASK]
    assert (task.status, task.settlement, task.charge_tx) == ("failed", "released", None)
    assert TASK not in execution_svc._live_runs


def test_a_failure_after_the_task_is_final_keeps_the_receipt(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    monkeypatch.setattr(dispute_store, "_store", _Store())

    async def _ratings(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("rating bug")

    monkeypatch.setattr(execution_svc, "_submit_ratings", _ratings)

    _start(_plan((0.01,), ("agt_ok",)), auth_id_hex=AUTH_ID_HEX, payer=PAYER)

    assert [name for name, _ in chain.calls] == ["settle", "seal"]
    assert _trace()[-1] == ("error", "workflow failed: rating bug", None)
    task = state.tasks[TASK]
    assert (task.status, task.charge_tx, task.settlement, task.spent_stroops) == (
        "complete",
        SETTLE_TX,
        "settled",
        100_000,
    )


def test_a_simulated_run_that_fails_is_finalised_with_what_it_had(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    def _broken(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("status bug")

    monkeypatch.setattr(execution_svc, "_terminal_status", _broken)

    _start(_plan((0.01,), ("agt_ok",)))

    assert _trace()[-1] == ("error", "workflow failed: status bug", None)
    task = state.tasks[TASK]
    assert (task.status, task.spent_stroops, task.artifact) == ("failed", 100_000, ARTIFACT)


def test_a_cancellation_mid_step_fails_the_task_and_releases_the_custody(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    monkeypatch.setattr(execution_svc, "STEP_TIMEOUT_SECONDS", 30.0)
    plan = _plan((0.01, 0.01), ("agt_ok", "agt_hang"))
    state.add_task(Task(id=TASK, intent=plan.intent, agents=2, spent=0.0, status="running"))

    async def scenario() -> None:
        run = asyncio.create_task(execution_svc._run(plan, TASK, auth_id_hex=AUTH_ID_HEX, payer=PAYER))
        while not harness["workers"]["agt_hang"].contexts:
            await asyncio.sleep(0.005)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(scenario())

    [release] = chain.named("settle")
    assert _payouts(release) == []
    assert _trace()[-2:] == [
        ("error", "workflow cancelled", None),
        ("cost", f"custody released to the buyer · tx {SETTLE_TX[:10]}…", "released"),
    ]
    task = state.tasks[TASK]
    # Finalised from what the run had: the delivered step's price and artifact.
    assert (task.status, task.spent_stroops, task.artifact, task.charge_tx) == ("failed", 0, ARTIFACT, None)


def test_a_cancellation_after_the_task_is_final_keeps_the_receipt_and_releases_nothing(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    monkeypatch.setattr(dispute_store, "_store", _Store())
    plan = _plan((0.01,), ("agt_ok",))
    state.add_task(Task(id=TASK, intent=plan.intent, agents=1, spent=0.0, status="running"))
    rating = asyncio.Event()

    async def _ratings(*args: Any, **kwargs: Any) -> None:
        rating.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(execution_svc, "_submit_ratings", _ratings)

    async def scenario() -> None:
        run = asyncio.create_task(execution_svc._run(plan, TASK, auth_id_hex=AUTH_ID_HEX, payer=PAYER))
        await rating.wait()
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(scenario())

    assert [name for name, _ in chain.calls] == ["settle", "seal"]
    assert _trace()[-1] == ("error", "workflow cancelled", None)
    task = state.tasks[TASK]
    assert (task.status, task.charge_tx, task.settlement, task.spent_stroops) == (
        "complete",
        SETTLE_TX,
        "settled",
        100_000,
    )


def test_a_signing_key_missing_skips_settle_without_a_release(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    monkeypatch.setattr(settings, "stellar_signing_key", "")

    _start(_plan((0.01,), ("agt_ok",)), auth_id_hex=AUTH_ID_HEX, payer=PAYER)

    assert chain.calls == []
    task = state.tasks[TASK]
    assert (task.status, task.settlement, task.charge_tx, task.spent_stroops) == ("complete", "failed", None, 0)


def test_a_kit_intent_is_traced_and_seeded_into_every_steps_context(harness: dict[str, Any]) -> None:
    plan = _plan((0.01,), ("agt_ok",)).model_copy(update={"intent": "build a snake game"})

    _start(plan)

    trace = _trace()
    assert trace[:3] == [
        ("input", "intent received → 'build a snake game'", None),
        ("exec", "kit detected: snake → VIPER·GRID (8 features locked)", None),
        ("exec", "orchestrator: decompose → [agt_ok]", None),
    ]
    context = harness["workers"]["agt_ok"].contexts[0]
    assert context["intent"] == "build a snake game"
    assert context["kit"]["kit_id"] == "snake"
