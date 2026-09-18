"""Reputation floor on the demo-kit planning path (story 3.01, BLO-23).

`_build_kit_plan` used to stamp reputation on every step but never call
`passes_floor`, so curated demo intents advertised a trust gate they did not
enforce — the most-watched path in the product. These tests pin the fix: the
floor is applied to every pipeline agent, a sub-floor agent is substituted or
dropped and the action is surfaced (never silent), the plan stays
deterministic with no LLM call, and the `_MIN_ROUTABLE_AGENTS` backstop keeps a
battered pipeline workable.
"""

from __future__ import annotations

import asyncio

import pytest

from app.demo_kits import detect_kit
from app.schemas import Agent
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.reputation_svc import RepInfo
from app.state import state

KIT_INTENT = "tetris game in html"


async def _noop(*_a: object, **_k: object) -> None:
    return None


def _sub_floor(agent_id: str, *, smoothed: int = 4000) -> RepInfo:
    """A rep entry that FAILS the routing floor (lower bound far under 5500)."""
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed,
        lower_bound_bps=100,
        avg_bps=smoothed,
        count=5,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> object:
    """Fresh 12-agent registry, restored after; kit thinking-sleep no-op'd."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(orchestrator_svc.asyncio, "sleep", _noop)
    yield
    state.agents.clear()
    state.agents.update(saved)


def _run_kit(reps: dict[str, RepInfo]) -> orchestrator_svc.DecomposeResponse:
    kit = detect_kit(KIT_INTENT)
    assert kit is not None
    return asyncio.run(orchestrator_svc._build_kit_plan(KIT_INTENT, kit, reps))


def test_sub_floor_agent_is_excluded_from_kit_plan(seeded: object) -> None:
    # agt_02k2 (design.figma) has no off-pipeline agent sharing its skills, so
    # a sub-floor rating drops it outright. The other five roles clear the
    # floor (cold start), so the backstop never fires.
    resp = _run_kit({"agt_02k2": _sub_floor("agt_02k2")})

    ids = [s.agent_id for s in resp.steps]
    assert "agt_02k2" not in ids
    assert len(resp.steps) == 5
    assert any(n.kind == "excluded" and n.agent_id == "agt_02k2" for n in resp.notices)


def test_sub_floor_agent_is_substituted_and_surfaced(seeded: object) -> None:
    # agt_05x7 (seo.brief) shares the "seo" skill with off-pipeline agt_01h8
    # (copywrite.v3), which clears the floor at cold start — so the role is
    # filled by a substitute rather than dropped, and the swap is recorded.
    resp = _run_kit({"agt_05x7": _sub_floor("agt_05x7")})

    ids = [s.agent_id for s in resp.steps]
    assert "agt_05x7" not in ids
    assert "agt_01h8" in ids
    assert len(resp.steps) == 6  # a substitution keeps the pipeline full

    step = next(s for s in resp.steps if s.agent_id == "agt_01h8")
    assert step.substituted_for == "agt_05x7"

    note = next(n for n in resp.notices if n.kind == "substituted")
    assert note.agent_id == "agt_05x7"
    assert note.replacement_id == "agt_01h8"
    assert "5500" in note.reason  # the notice names the floor it failed


def test_kit_plan_is_deterministic(seeded: object) -> None:
    # Same reputation state, twice: identical steps, substitutions, and
    # notices. plan_id is a random token so it is excluded from the compare.
    reps = {"agt_05x7": _sub_floor("agt_05x7"), "agt_02k2": _sub_floor("agt_02k2")}
    first = _run_kit(reps)
    second = _run_kit(reps)

    assert [s.agent_id for s in first.steps] == [s.agent_id for s in second.steps]
    assert [s.substituted_for for s in first.steps] == [s.substituted_for for s in second.steps]
    assert [(n.kind, n.agent_id, n.replacement_id) for n in first.notices] == [
        (n.kind, n.agent_id, n.replacement_id) for n in second.notices
    ]
    assert first.total_usdc == second.total_usdc
    assert first.total_eta == second.total_eta


def test_kit_path_applies_floor_without_calling_the_llm(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # Drive the full public entry point: decompose() must detect the kit,
    # apply the floor, and never touch the orchestrator LLM. Any call to it
    # fails the test. Recorded rather than raised: decompose degrades a planner
    # call that raises to its fallback plan (BLO-121), so a raise here would be
    # swallowed and a kit path that wrongly reached the model would still pass.
    llm_calls: list[object] = []

    async def _record(*a: object, **_k: object) -> object:
        llm_calls.append(a)
        return None

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _record)

    reps = {"agt_05x7": _sub_floor("agt_05x7")}

    async def _fake_reps(_ids: object, *_a: object, **_k: object) -> dict[str, RepInfo]:
        return reps

    monkeypatch.setattr(orchestrator_svc.reputation_svc, "fetch_reps", _fake_reps)

    resp = asyncio.run(orchestrator_svc.decompose(KIT_INTENT))
    assert llm_calls == [], "kit path must never call the LLM"

    ids = [s.agent_id for s in resp.steps]
    assert "agt_05x7" not in ids
    assert "agt_01h8" in ids
    assert any(n.kind == "substituted" and n.agent_id == "agt_05x7" for n in resp.notices)


def test_starvation_backstop_keeps_kit_plan_workable(seeded: object) -> None:
    # Every kit agent falls below the floor at once. Only agt_05x7 has a
    # matching off-pipeline substitute (agt_01h8), leaving one step — under the
    # _MIN_ROUTABLE_AGENTS floor of 3. The backstop must re-admit two dropped
    # agents — the builder, then the best-scored — so the buyer still gets a
    # workable plan, recording the degradation; the rest are recorded as
    # exclusions.
    scores = {
        "agt_09l5": 5000,
        "agt_05x7": 4600,
        "agt_02k2": 4000,
        "agt_11c0": 4900,
        "agt_12r0": 4800,
        "agt_08j2": 4700,
    }
    resp = _run_kit({aid: _sub_floor(aid, smoothed=s) for aid, s in scores.items()})

    ids = [s.agent_id for s in resp.steps]
    assert len(resp.steps) >= orchestrator_svc._MIN_ROUTABLE_AGENTS
    assert "agt_01h8" in ids  # the one substitution still stands
    # Pipeline order throughout: research, then the substitute in the brand
    # slot it fills, then the builder — never in the order they were admitted.
    assert ids == ["agt_09l5", "agt_01h8", "agt_11c0"]

    # code.gen and the best-scored other role are re-admitted, not the rest.
    degraded = {n.agent_id for n in resp.notices if n.kind == "degraded"}
    assert degraded == {"agt_09l5", "agt_11c0"}
    assert {"agt_09l5", "agt_11c0"} <= set(ids)

    # Re-admitted steps carry the inline degraded flag; the substitute does not.
    by_id = {s.agent_id: s for s in resp.steps}
    assert by_id["agt_09l5"].degraded is True
    assert by_id["agt_11c0"].degraded is True
    assert by_id["agt_01h8"].degraded is False

    # Everything else the floor removed is surfaced as an exclusion, never
    # silently gone.
    excluded = {n.agent_id for n in resp.notices if n.kind == "excluded"}
    assert excluded == {"agt_02k2", "agt_12r0", "agt_08j2"}


def test_kit_step_for_unseeded_agent_is_skipped_not_crashed(seeded: object) -> None:
    # A kit pipeline id missing from the registry is a programmer error, not a
    # floor action: the step is skipped (no notice) and the plan still builds.
    state.agents.pop("agt_08j2", None)

    resp = _run_kit({})

    ids = [s.agent_id for s in resp.steps]
    assert "agt_08j2" not in ids
    assert len(resp.steps) == 5
    assert not any(n.agent_id == "agt_08j2" for n in resp.notices)


def test_backstop_re_admits_code_gen_before_higher_scored_roles(seeded: object) -> None:
    # The audit's case. Every kit agent is under the floor and so is the
    # copywriter, so no role has a substitute and all six are dropped. By score
    # alone the backstop re-admitted research + brand + tokens: three paid
    # steps preparing inputs for a build no step performs, and no artifact.
    # The builder role outranks score, then the shared ranking fills the rest.
    scores = {
        "agt_09l5": 5000,
        "agt_05x7": 4900,
        "agt_02k2": 4800,
        "agt_11c0": 4000,
        "agt_12r0": 3900,
        "agt_08j2": 3800,
        "agt_01h8": 3000,
    }
    resp = _run_kit({aid: _sub_floor(aid, smoothed=s) for aid, s in scores.items()})

    assert [s.agent_id for s in resp.steps] == ["agt_09l5", "agt_05x7", "agt_11c0"]
    assert all(s.degraded for s in resp.steps)
    assert {n.agent_id for n in resp.notices if n.kind == "degraded"} == {"agt_09l5", "agt_05x7", "agt_11c0"}
    assert {n.agent_id for n in resp.notices if n.kind == "excluded"} == {"agt_02k2", "agt_12r0", "agt_08j2"}


def test_re_admitted_kit_steps_keep_their_pipeline_position(seeded: object) -> None:
    # code.gen clears the floor (no entry == cold start) and every other role
    # is dropped, so the backstop re-admits two: tokens and research, the two
    # best scores. Appended after the loop they used to land AFTER code.gen,
    # and execution runs steps in list order — code.gen would build before the
    # design tokens it reads from the run context existed.
    scores = {
        "agt_09l5": 4900,
        "agt_05x7": 4000,
        "agt_02k2": 5000,
        "agt_12r0": 3900,
        "agt_08j2": 3800,
        "agt_01h8": 3000,
    }
    resp = _run_kit({aid: _sub_floor(aid, smoothed=s) for aid, s in scores.items()})

    ids = [s.agent_id for s in resp.steps]
    assert ids == ["agt_09l5", "agt_02k2", "agt_11c0"]
    assert ids.index("agt_02k2") < ids.index("agt_11c0")
    assert [s.degraded for s in resp.steps] == [True, True, False]


def test_kit_path_reports_unbound_agents_after_its_floor_notices(seeded: object) -> None:
    # An on-chain agent indexed but never bound: marketplace-visible, not
    # dispatchable. The free-form path has reported it since story 3.02; the
    # kit path said nothing, so the same registry read differently depending on
    # which path planned the intent. Same selection, same place in the list.
    state.add_agent(
        Agent(
            id="ext_idx1",
            name="ext_idx1.remote",
            skills=["remote"],
            price=0.02,
            rep=4.99,
            status="online",
            runs=0,
            source="onchain",
        )
    )

    resp = _run_kit({"agt_02k2": _sub_floor("agt_02k2")})

    assert [(n.kind, n.reason_code, n.agent_id) for n in resp.notices] == [
        ("excluded", "below_floor", "agt_02k2"),
        ("excluded", "unbound_endpoint", "ext_idx1"),
    ]
    # Reported, never routed: nothing can execute a step for it.
    assert "ext_idx1" not in [s.agent_id for s in resp.steps]
