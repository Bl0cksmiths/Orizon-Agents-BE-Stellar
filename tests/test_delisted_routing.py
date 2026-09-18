"""Delisting an agent actually stops work being routed to it.

`AgentRegistry.set_active(agent_id, false)` is the only control an operator has
for taking their agent out of service. `registry_sync` maps it to
`Agent.status == "offline"` and, until this change, nothing in routing read that
field — the sole consumer of `Agent.status` in the backend was a metrics
counter. A delisted agent kept being offered to the planner and kept receiving
paid work, so the operator's kill switch was wired to nothing.

These tests pin the switch on both planning paths, and pin the two ways it could
be quietly unwired again:

  * the starvation backstop relaxes the REPUTATION FLOOR when too few agents
    clear it. The floor is our rule and we may bend it; a delisting is the
    operator's decision about their own service and may not be bent, however
    good the withdrawn agent's score is;
  * `_floor_substitute` promotes an off-pipeline agent into a kit slot. A
    delisted agent promoted there is the same defect wearing a different hat.

They also pin the REPORTING decision: a withdrawn agent produces no
`PlanFloorNotice` under any reason code. ADR 0006 D2 left `inactive` out of the
closed vocabulary because routing could not produce the state; this change makes
it producible, and the answer is still no — see `_routable_registry` for the
argument. Silence is a contract here, not an omission, so it is asserted rather
than assumed.

The predicate is `status != "offline"`, never `status == "online"`: the seeded
catalog ships `agt_04m1` and `agt_06q4` as "idle", which means idle, and the
twelve-agent demo catalog must not shrink to enforce a flag nobody set.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.demo_kits import detect_kit
from app.schemas import Agent, DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.reputation_svc import RepInfo
from app.state import state

# Matches no DemoKit, so decompose() takes the free-form model path.
FREE_FORM_INTENT = "write a haiku about databases"
KIT_INTENT = "tetris game in html"

# Seeded agents shipped as "idle" — available, not withdrawn.
IDLE_SEEDED = ("agt_04m1", "agt_06q4")


async def _noop(*_a: object, **_k: object) -> None:
    return None


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> object:
    """Fresh 12-agent registry, restored after; kit thinking-sleep no-op'd.

    `seed_registry` builds new `Agent` objects, so the delisting helpers below
    mutate copies this fixture throws away — never the objects other suites
    hold.
    """
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(orchestrator_svc.asyncio, "sleep", _noop)
    yield
    state.agents.clear()
    state.agents.update(saved)


def _set_status(agent_id: str, status: str) -> None:
    state.add_agent(state.agents[agent_id].model_copy(update={"status": status}))


def _delist(*agent_ids: str) -> None:
    """What `set_active(id, false)` looks like after a registry-sync pass."""
    for agent_id in agent_ids:
        _set_status(agent_id, "offline")


def _relist(*agent_ids: str) -> None:
    for agent_id in agent_ids:
        _set_status(agent_id, "online")


def _info(agent_id: str, *, smoothed: int, lower: int) -> RepInfo:
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed,
        lower_bound_bps=lower,
        avg_bps=smoothed,
        count=5,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


def _sub_floor(agent_id: str, *, smoothed: int = 4000) -> RepInfo:
    """A rep entry that FAILS the routing floor (lower bound far under 5500)."""
    return _info(agent_id, smoothed=smoothed, lower=100)


def _clearing_reps() -> dict[str, RepInfo]:
    return {a.id: _info(a.id, smoothed=8000, lower=8000) for a in state.list_agents()}


def _offered_ids(reps: dict[str, RepInfo]) -> list[str]:
    """The agent ids in the AVAILABLE_AGENTS block, in the order shown."""
    block = orchestrator_svc._registry_prompt_fragment(reps)
    return [ln.split(" ")[1].removeprefix("id=") for ln in block.splitlines() if ln.startswith("- id=")]


def _run_kit(reps: dict[str, RepInfo]) -> DecomposeResponse:
    kit = detect_kit(KIT_INTENT)
    assert kit is not None
    return asyncio.run(orchestrator_svc._build_kit_plan(KIT_INTENT, kit, reps))


def _plan_naming(*agent_ids: str) -> object:
    async def _arun(_prompt: str) -> object:
        steps = [
            PlanStep(agent_id=aid, rationale="model-chosen step", est_price_usdc=0.05, est_eta_seconds=1.0)
            for aid in agent_ids
        ]
        return SimpleNamespace(content=Plan(steps=steps))

    return _arun


def _decompose(
    monkeypatch: pytest.MonkeyPatch,
    reps: dict[str, RepInfo],
    *picks: str,
) -> DecomposeResponse:
    async def _fake_reps(_ids: object, *_a: object, **_k: object) -> dict[str, RepInfo]:
        return reps

    monkeypatch.setattr(orchestrator_svc.reputation_svc, "fetch_reps", _fake_reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _plan_naming(*picks))
    return asyncio.run(orchestrator_svc.decompose(FREE_FORM_INTENT))


# ── the seeded catalog must survive the predicate ───────────────


def test_idle_seeded_agents_are_still_offered(seeded: object) -> None:
    # The hard constraint on the predicate. "idle" is a seeded liveness hint
    # meaning "nothing in flight", and `registry_sync` never produces it —
    # `set_active` yields only "online" or "offline". A `status == "online"`
    # rule would read two working demo agents as withdrawn and shrink the
    # twelve-agent catalog to ten to enforce a flag neither operator ever set.
    offered = _offered_ids(_clearing_reps())

    assert len(offered) == 12
    for agent_id in IDLE_SEEDED:
        assert state.agents[agent_id].status == "idle"
        assert agent_id in offered


def test_seeded_catalog_still_plans_a_full_kit(seeded: object) -> None:
    resp = _run_kit({})

    assert [s.agent_id for s in resp.steps] == [aid for aid, _ in orchestrator_svc._KIT_PIPELINE]
    assert resp.notices == []


# ── the model path ──────────────────────────────────────────────


def test_delisted_agent_is_not_offered_to_the_planner(seeded: object) -> None:
    # Top score in the registry, so nothing but the listing flag is keeping it
    # out of the block.
    reps = _clearing_reps()
    reps["agt_03d9"] = _info("agt_03d9", smoothed=9999, lower=9999)
    _delist("agt_03d9")

    offered = _offered_ids(reps)

    assert "agt_03d9" not in offered
    assert len(offered) == 11


def test_starvation_backstop_never_re_admits_a_delisted_agent(seeded: object) -> None:
    # Nobody clears the floor, so the backstop re-sorts the whole dispatchable
    # set by smoothed score and takes the top three. The delisted agent has the
    # best score by a wide margin and would head that sort. The floor may be
    # relaxed under starvation; an operator's withdrawal may not be.
    reps = {a.id: _sub_floor(a.id, smoothed=1000 + i * 10) for i, a in enumerate(state.list_agents())}
    reps["agt_03d9"] = _sub_floor("agt_03d9", smoothed=9999)
    _delist("agt_03d9")

    offered = _offered_ids(reps)

    assert len(offered) == orchestrator_svc._MIN_ROUTABLE_AGENTS
    assert "agt_03d9" not in offered


def test_delisted_agent_produces_no_notice(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # The reporting decision, pinned. A withdrawn agent is absent, not refused:
    # it was not judged by the floor, so reporting it as `below_floor` would be
    # a false accusation, and no other reason code in the closed vocabulary
    # describes it either. Silence is the contract.
    _delist("agt_03d9")

    resp = _decompose(monkeypatch, _clearing_reps(), "agt_11c0")

    assert [s.agent_id for s in resp.steps] == ["agt_11c0"]
    assert resp.notices == []


def test_delisted_agent_is_not_reported_as_below_floor(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # Delisted AND sub-floor. Both facts are true; only one of them is why the
    # agent is absent, and it is the one the floor did not decide.
    reps = _clearing_reps()
    reps["agt_03d9"] = _sub_floor("agt_03d9")
    _delist("agt_03d9")

    resp = _decompose(monkeypatch, reps, "agt_11c0")

    assert resp.notices == []


def test_delisted_unbound_agent_is_not_reported_as_unbound_endpoint(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An on-chain agent with no binding is normally reported so the buyer can
    # see why the marketplace listed more agents than the plan drew from
    # (ADR 0006 D4). A withdrawn one is not that gap: "no endpoint bound" is
    # true of it but is not why it is absent, and it is advice nobody wants
    # acted on. The still-listed one beside it proves the filter is the
    # delisting and not the notice builder going quiet.
    for agent_id, status in (("ext_idx1", "offline"), ("ext_idx2", "online")):
        state.add_agent(
            Agent(
                id=agent_id,
                name=f"{agent_id}.remote",
                skills=["remote"],
                price=0.02,
                rep=4.99,
                status=status,  # type: ignore[arg-type]
                runs=0,
                source="onchain",
            )
        )

    resp = _decompose(monkeypatch, _clearing_reps(), "agt_11c0")

    assert [n.agent_id for n in resp.notices] == ["ext_idx2"]
    assert resp.notices[0].reason_code == "unbound_endpoint"


def test_clamp_drops_a_model_step_naming_a_delisted_agent(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # The planner is never shown a delisted agent, but it can name one anyway —
    # from an earlier turn, or by inventing an id that happens to exist. The
    # clamp is the last gate before the step is stored and later dispatched.
    _delist("agt_03d9")

    resp = _decompose(monkeypatch, _clearing_reps(), "agt_03d9", "agt_11c0")

    assert [s.agent_id for s in resp.steps] == ["agt_11c0"]
    assert [s.agent_id for s in state.plans[resp.plan_id].plan.steps] == ["agt_11c0"]


def test_clamp_falls_back_when_the_model_names_only_a_delisted_agent(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Everything clamped away leaves the minimal safe plan rather than a plan
    # of steps /execute would skip.
    _delist("agt_03d9")

    resp = _decompose(monkeypatch, _clearing_reps(), "agt_03d9")

    assert [s.agent_id for s in resp.steps] == ["agt_01h8"]


def test_relisting_restores_routability(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # `set_active(id, true)` again. Nothing here caches the verdict, so the
    # next plan sees the agent back.
    reps = _clearing_reps()
    _delist("agt_03d9")
    assert "agt_03d9" not in _offered_ids(reps)

    _relist("agt_03d9")

    assert "agt_03d9" in _offered_ids(reps)
    assert [s.agent_id for s in _decompose(monkeypatch, reps, "agt_03d9").steps] == ["agt_03d9"]


# ── the kit path ────────────────────────────────────────────────


def test_delisted_kit_agent_is_dropped_without_a_substitute(seeded: object) -> None:
    # agt_05x7 (seo.brief) shares the "seo" skill with off-pipeline agt_01h8,
    # so a SUB-FLOOR rating would have its role filled by a substitute. A
    # delisting must not: substitution is a floor action, and quietly promoting
    # a stand-in with no notice is the silently reshuffled pipeline story 3.02
    # forbids.
    _delist("agt_05x7")

    resp = _run_kit({})

    ids = [s.agent_id for s in resp.steps]
    assert "agt_05x7" not in ids
    assert "agt_01h8" not in ids
    assert len(resp.steps) == 5
    assert resp.notices == []


def test_delisted_agent_is_never_chosen_as_a_floor_substitute(seeded: object) -> None:
    # The pool `_floor_substitute` draws from. agt_01h8 is the only
    # off-pipeline agent sharing a skill with sub-floor agt_05x7, so with it
    # withdrawn the role has no stand-in and is dropped — reported, because
    # THAT exclusion really was the floor's verdict.
    _delist("agt_01h8")

    resp = _run_kit({"agt_05x7": _sub_floor("agt_05x7")})

    ids = [s.agent_id for s in resp.steps]
    assert "agt_01h8" not in ids
    assert "agt_05x7" not in ids
    assert len(resp.steps) == 5
    assert [(n.kind, n.agent_id, n.reason_code) for n in resp.notices] == [("excluded", "agt_05x7", "below_floor")]


def test_kit_starvation_backstop_never_re_admits_a_delisted_agent(seeded: object) -> None:
    # The kit backstop re-admits dropped agents below the floor when too few
    # steps survive. A delisted agent never enters the dropped pool at all, so
    # no score can pull it back in — both withdrawn agents here carry the top
    # smoothed scores in the snapshot.
    _delist("agt_09l5", "agt_11c0")
    reps = {
        "agt_09l5": _sub_floor("agt_09l5", smoothed=9999),
        "agt_11c0": _sub_floor("agt_11c0", smoothed=9998),
        "agt_05x7": _sub_floor("agt_05x7", smoothed=4600),
        "agt_02k2": _sub_floor("agt_02k2", smoothed=4000),
        "agt_12r0": _sub_floor("agt_12r0", smoothed=4800),
        "agt_08j2": _sub_floor("agt_08j2", smoothed=4700),
    }

    resp = _run_kit(reps)

    ids = [s.agent_id for s in resp.steps]
    # agt_05x7's role goes to its substitute; the backstop then re-admits the
    # two highest-scored DROPPED agents to reach _MIN_ROUTABLE_AGENTS.
    assert ids == ["agt_01h8", "agt_12r0", "agt_08j2"]
    assert len(resp.steps) >= orchestrator_svc._MIN_ROUTABLE_AGENTS
    for agent_id in ("agt_09l5", "agt_11c0"):
        assert agent_id not in ids
        assert not any(n.agent_id == agent_id for n in resp.notices)
    assert {n.agent_id for n in resp.notices if n.kind == "degraded"} == {"agt_12r0", "agt_08j2"}


def test_kit_plan_stays_deterministic_with_a_delisted_agent(seeded: object) -> None:
    # Same intent, same state, same plan — with no LLM call. plan_id is a
    # random token, so it is excluded from the compare.
    _delist("agt_05x7", "agt_08j2")
    reps = {"agt_02k2": _sub_floor("agt_02k2")}

    first, second = _run_kit(reps), _run_kit(reps)

    assert [s.agent_id for s in first.steps] == [s.agent_id for s in second.steps]
    assert [(n.kind, n.agent_id, n.replacement_id) for n in first.notices] == [
        (n.kind, n.agent_id, n.replacement_id) for n in second.notices
    ]
    assert (first.total_usdc, first.total_eta) == (second.total_usdc, second.total_eta)


def test_relisting_restores_the_full_kit_pipeline(seeded: object) -> None:
    _delist("agt_05x7")
    assert len(_run_kit({}).steps) == 5

    _relist("agt_05x7")

    assert [s.agent_id for s in _run_kit({}).steps] == [aid for aid, _ in orchestrator_svc._KIT_PIPELINE]


def test_kit_path_honours_delisting_without_calling_the_llm(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # Recorded rather than raised: decompose degrades a planner call that
    # raises to its fallback plan (BLO-121), which would swallow the raise.
    llm_calls: list[object] = []

    async def _record(*a: object, **_k: object) -> object:
        llm_calls.append(a)
        return None

    async def _fake_reps(_ids: object, *_a: object, **_k: object) -> dict[str, RepInfo]:
        return {}

    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _record)
    monkeypatch.setattr(orchestrator_svc.reputation_svc, "fetch_reps", _fake_reps)
    _delist("agt_11c0")

    resp = asyncio.run(orchestrator_svc.decompose(KIT_INTENT))
    assert llm_calls == [], "kit path must never call the LLM"

    assert "agt_11c0" not in [s.agent_id for s in resp.steps]
    assert resp.notices == []
