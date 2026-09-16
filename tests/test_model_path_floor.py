"""Reputation-floor visibility on the FREE-FORM planning path (story 3.02).

The curated demo-kit path has reported floor actions since story 3.01, but the
model path — every non-curated intent, i.e. the normal product — built its
`DecomposeResponse` with no notices at all: `_registry_prompt_fragment`
computed the routable set, threw the complement away, and logged the starvation
relaxation to a server log no buyer will ever read. Demo a tetris intent and
the feature looked finished.

The first test here is a PIN, not a feature test. Surfacing the discarded
complement means changing how the routable set is computed, and the planning
prompt is the one string in this service whose exact bytes decide what the LLM
plans. If it shifts by a character the plans shift with it and every downstream
assertion drifts for a reason no failure message would name. So the block is
pinned literally, before the refactor, in both of its shapes: the ordinary
floor-filtered listing and the starvation backstop's top-N.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import settings
from app.schemas import Agent, DecomposeResponse, Plan, PlanStep
from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.plan_notices import UNBOUND_REPORT_CAP
from app.services.reputation_svc import RepInfo
from app.state import state

# Matches no DemoKit, so decompose() takes the free-form model path.
FREE_FORM_INTENT = "write a haiku about databases"


def _info(agent_id: str, *, smoothed: int, lower: int, degraded: bool = False) -> RepInfo:
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed,
        lower_bound_bps=lower,
        avg_bps=smoothed,
        count=3,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
        degraded=degraded,
    )


@pytest.fixture()
def seeded() -> object:
    """Fresh 12-agent registry, restored after — every seeded agent has a local
    worker, so the whole catalog is dispatchable and the prompt block below is
    a function of the reputation snapshot alone."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    yield
    state.agents.clear()
    state.agents.update(saved)


# Rep snapshot for the pin: every agent rated, scores fanned out so ordering is
# observable, with two deliberate holes —
#   * agt_04m1's lower bound is under the 5500 floor, so it is filtered out;
#   * agt_06q4 has no entry at all, so its line must fall back to the registry's
#     seeded `rep` (4.71) rather than a smoothed score.
def _pinned_reps() -> dict[str, RepInfo]:
    reps = {a.id: _info(a.id, smoothed=6000 + i * 100, lower=6000) for i, a in enumerate(state.list_agents())}
    reps["agt_04m1"] = _info("agt_04m1", smoothed=6300, lower=100)
    del reps["agt_06q4"]
    return reps


# Byte-for-byte, including the `name="…"` quoting the prompt-injection fence
# adds, the 3-decimal price, the 2-decimal rep on the 0–5 scale, and the
# comma-joined skills. Written out rather than rebuilt from a format string: a
# pin that recomputes the thing it pins cannot catch the thing it is for.
PINNED_BLOCK = """AVAILABLE_AGENTS:
- id=agt_01h8 name="copywrite.v3" price=0.012 rep=3.00 skills=copy,seo,en
- id=agt_02k2 name="design.figma" price=0.018 rep=3.05 skills=ui,tokens,figma
- id=agt_03d9 name="code.next" price=0.066 rep=3.10 skills=ts,react,next
- id=agt_05x7 name="seo.brief" price=0.009 rep=3.20 skills=seo,research
- id=agt_06q4 name="vision.ocr" price=0.014 rep=4.71 skills=vision,ocr
- id=agt_07w3 name="ads.meta" price=0.022 rep=3.30 skills=ads,meta
- id=agt_08j2 name="deploy.v0" price=0.011 rep=3.35 skills=deploy,ci,seal
- id=agt_09l5 name="research.pro" price=0.024 rep=3.40 skills=research,citations
- id=agt_10b6 name="translate.42" price=0.007 rep=3.45 skills=i18n,42 langs
- id=agt_11c0 name="code.gen" price=0.054 rep=3.50 skills=code,html,js,build
- id=agt_12r0 name="code.critic" price=0.052 rep=3.55 skills=a11y,polish,review"""

# The starvation backstop's shape: nobody clears the floor, so the block is the
# top _MIN_ROUTABLE_AGENTS by SMOOTHED score (not lower bound), best first —
# an ordering the planner reads as a ranking, so it is pinned too.
PINNED_STARVED_BLOCK = """AVAILABLE_AGENTS:
- id=agt_12r0 name="code.critic" price=0.052 rep=0.56 skills=a11y,polish,review
- id=agt_11c0 name="code.gen" price=0.054 rep=0.55 skills=code,html,js,build
- id=agt_10b6 name="translate.42" price=0.007 rep=0.55 skills=i18n,42 langs"""


def test_registry_prompt_block_is_byte_identical(seeded: object) -> None:
    assert orchestrator_svc._registry_prompt_fragment(_pinned_reps()) == PINNED_BLOCK


def test_starved_registry_prompt_block_is_byte_identical(seeded: object) -> None:
    reps = {a.id: _info(a.id, smoothed=1000 + i * 10, lower=100) for i, a in enumerate(state.list_agents())}

    assert orchestrator_svc._registry_prompt_fragment(reps) == PINNED_STARVED_BLOCK


def test_registry_prompt_block_is_stable_across_runs(seeded: object) -> None:
    # Nothing in the block may depend on set iteration, dict ordering or a
    # clock: the same snapshot must render the same bytes every time.
    reps = _pinned_reps()

    assert orchestrator_svc._registry_prompt_fragment(reps) == orchestrator_svc._registry_prompt_fragment(reps)


# ── the model path ──────────────────────────────────────────────
# Everything below drives the real `decompose()` entry point with the LLM and
# the reputation read stubbed out, because the defect was never in how the
# routable set is computed — it was in what reached the RESPONSE.


def _plan_naming(*agent_ids: str) -> object:
    """A stand-in planner that returns a plan naming exactly `agent_ids`."""

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


def _clearing_reps() -> dict[str, RepInfo]:
    """Everyone comfortably over the floor — no starvation, no exclusions."""
    return {a.id: _info(a.id, smoothed=8000, lower=8000) for a in state.list_agents()}


def _add_agent(agent_id: str, *, source: str) -> None:
    """A registry entry with no local worker and no binding, so it is not
    dispatchable. `ext_` namespace on purpose — `agt_` is the seeded catalog."""
    state.add_agent(
        Agent(
            id=agent_id,
            name=f"{agent_id}.remote",
            skills=["remote"],
            price=0.02,
            rep=4.99,
            status="online",
            runs=0,
            source=source,  # type: ignore[arg-type]
        )
    )


def test_sub_floor_agent_is_reported_with_the_deciding_numbers(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # One agent under the floor, eleven over it, so the starvation backstop
    # stays out of this and the exclusion is the floor's own verdict.
    reps = _clearing_reps()
    reps["agt_04m1"] = _info("agt_04m1", smoothed=6300, lower=4100)

    resp = _decompose(monkeypatch, reps, "agt_11c0")

    note = next(n for n in resp.notices if n.agent_id == "agt_04m1")
    assert note.kind == "excluded"
    assert note.reason_code == "below_floor"
    assert note.agent_name == "sol-audit"
    # The numbers as data, not only interpolated into the sentence: a card that
    # wants to render "4.10 against a 5.50 floor" should not parse English.
    assert note.lower_bound_bps == 4100
    assert note.floor_bps == 5500
    assert "4100 < 5500" in note.reason
    # …and it is the ONLY notice. Eleven agents cleared the floor and ten of
    # them were not picked; none of that is an exclusion.
    assert [n.agent_id for n in resp.notices] == ["agt_04m1"]


def test_agent_that_cleared_the_floor_but_was_not_picked_gets_no_notice(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The signal this story exists to create is "the floor removed something".
    # Listing the eleven agents the planner simply did not choose would bury it
    # under the registry, which is why `not_selected_by_planner` is deliberately
    # absent from the ExclusionReason vocabulary.
    resp = _decompose(monkeypatch, _clearing_reps(), "agt_11c0")

    assert [s.agent_id for s in resp.steps] == ["agt_11c0"]
    assert resp.notices == []


def test_unbound_on_chain_agent_is_reported_as_unbound_endpoint(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Indexed from the chain (story 1.02) but never bound to an endpoint
    # (story 2.01): marketplace-visible, un-routable, and until now invisible
    # to the buyer wondering where it went.
    _add_agent("ext_idx1", source="onchain")

    resp = _decompose(monkeypatch, _clearing_reps(), "agt_11c0")

    note = next(n for n in resp.notices if n.agent_id == "ext_idx1")
    assert note.kind == "excluded"
    assert note.reason_code == "unbound_endpoint"
    # Not a reputation verdict, so there is no deciding bound to show — but the
    # plan's floor is stamped anyway, as on every other notice.
    assert note.lower_bound_bps is None
    assert note.floor_bps == 5500
    assert [n.agent_id for n in resp.notices] == ["ext_idx1"]


def test_unbound_seeded_agent_is_not_reported(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every seeded agent ships with a local worker, so one that is not
    # dispatchable is a broken deployment, not a marketplace agent awaiting a
    # bind. Telling the buyer to go and bind it would be advice they cannot act
    # on about an agent they never registered.
    _add_agent("ext_seed1", source="seeded")

    resp = _decompose(monkeypatch, _clearing_reps(), "agt_11c0")

    assert resp.notices == []


def test_unbound_report_is_capped(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # The registry is permissionless, so the unbound set grows without limit
    # while a plan's worth of genuine near-misses stays around six.
    for i in range(UNBOUND_REPORT_CAP + 5):
        _add_agent(f"ext_idx{i:02d}", source="onchain")

    resp = _decompose(monkeypatch, _clearing_reps(), "agt_11c0")

    assert len(resp.notices) == UNBOUND_REPORT_CAP
    # Sorted by id then capped, so which eight are named is a property of the
    # input set rather than of registry insertion order.
    assert [n.agent_id for n in resp.notices] == [f"ext_idx{i:02d}" for i in range(UNBOUND_REPORT_CAP)]


def test_starvation_relaxation_is_reported_as_floor_relaxed(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nobody clears the floor, so the backstop re-admits the top three by
    # smoothed score to keep the planner from having nothing to route to. That
    # used to be a logger.warning on a server nobody reads.
    reps = {a.id: _info(a.id, smoothed=1000 + i * 10, lower=100) for i, a in enumerate(state.list_agents())}

    resp = _decompose(monkeypatch, reps, "agt_12r0")

    relaxed = [n for n in resp.notices if n.reason_code == "floor_relaxed"]
    assert {n.agent_id for n in relaxed} == {"agt_12r0", "agt_11c0", "agt_10b6"}
    for n in relaxed:
        # Re-admitted, so it is IN the plan's shortlist — `kind` says what
        # happened to the plan, `reason_code` says why.
        assert n.kind == "degraded"
        assert n.lower_bound_bps == 100
        assert n.floor_bps == 5500
        assert "fewer than 3" in n.reason

    # The nine the backstop did not reach stay plain below-floor exclusions.
    excluded = {n.agent_id for n in resp.notices if n.reason_code == "below_floor"}
    assert excluded == {a.id for a in state.list_agents()} - {"agt_12r0", "agt_11c0", "agt_10b6"}


def test_floor_bps_states_the_threshold_this_plan_was_built_under(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Configurable per deployment, so a client that hardcoded 5500 would
    # narrate the wrong number after a tune. Present even on a clean plan.
    resp = _decompose(monkeypatch, _clearing_reps(), "agt_11c0")
    assert resp.notices == []
    assert resp.floor_bps == settings.reputation_floor_bps == 5500

    monkeypatch.setattr(settings, "reputation_floor_bps", 7000)
    assert _decompose(monkeypatch, _clearing_reps(), "agt_11c0").floor_bps == 7000


def test_reputation_degraded_tracks_the_failed_read_not_the_relaxed_floor(
    seeded: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `RepInfo.degraded` means the LEDGER READ FAILED and the Bayesian prior was
    # served instead. It is not `PlanFloorNotice.kind == "degraded"`, which
    # means "re-admitted below the floor", and the two must not be wired to
    # each other — the whole point of the flag is that the floor verdicts in
    # this plan rest on an estimate.
    assert _decompose(monkeypatch, _clearing_reps(), "agt_11c0").reputation_degraded is False

    # A plan FULL of kind="degraded" notices, with every read healthy.
    starved = {a.id: _info(a.id, smoothed=1000 + i * 10, lower=100) for i, a in enumerate(state.list_agents())}
    resp = _decompose(monkeypatch, starved, "agt_12r0")
    assert any(n.kind == "degraded" for n in resp.notices)
    assert resp.reputation_degraded is False

    # One failed read is enough, even with no floor action anywhere.
    reps = _clearing_reps()
    reps["agt_03d9"] = _info("agt_03d9", smoothed=7000, lower=7000, degraded=True)
    resp = _decompose(monkeypatch, reps, "agt_11c0")
    assert resp.notices == []
    assert resp.reputation_degraded is True


def test_notice_order_is_stable_across_runs(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # The plan card renders these in sequence, so a list that reshuffles
    # between two identical requests reads as the system changing its mind.
    # All three groups at once: exclusions, relaxations, unbound.
    _add_agent("ext_idx2", source="onchain")
    _add_agent("ext_idx1", source="onchain")
    reps = {a.id: _info(a.id, smoothed=1000 + i * 10, lower=100) for i, a in enumerate(state.list_agents())}

    first = _decompose(monkeypatch, reps, "agt_12r0")
    second = _decompose(monkeypatch, reps, "agt_12r0")
    shape = [(n.kind, n.reason_code, n.agent_id) for n in first.notices]

    assert shape == [(n.kind, n.reason_code, n.agent_id) for n in second.notices]
    # Grouped by what the buyer needs first: what the floor removed, then what
    # it let through anyway, then the registry entries nothing could dispatch.
    codes = [c for _, c, _ in shape]
    assert codes == sorted(codes, key=["below_floor", "floor_relaxed", "unbound_endpoint"].index)
    assert [aid for _, c, aid in shape if c == "unbound_endpoint"] == ["ext_idx1", "ext_idx2"]
