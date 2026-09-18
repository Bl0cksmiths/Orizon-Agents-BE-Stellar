"""Both planning paths must report the reputation floor the same way (story 3.02).

Story 3.01 taught the curated demo-kit path to apply the floor and say so. The
free-form path applies the same floor — `passes_floor` filters the routing
prompt — and says nothing at all. So the feature exists only on the one intent
a reviewer is most likely to type: "tetris game in html" returns exclusions,
and every other intent returns a plan with the same agents quietly missing
from it. A buyer cannot tell a plan that considered an agent and rejected it
from one that never saw it.

These are the CONTRACT tests for the fix. They drive `decompose()` — the
public entry point both paths share — and assert on the RESPONSE only, never
on the helpers that build it, so the free-form path's prompt construction can
be restructured underneath them without touching this file.

What they pin:

  * the two paths report the same floor action, in the same shape, with the
    same closed reason vocabulary, from one shared reputation snapshot;
  * the floor a plan was actually built against travels on the response, read
    from settings rather than assumed by the client;
  * an agent the planner simply did not choose is NOT an exclusion;
  * every field this story adds is optional, so a pre-3.02 client still
    validates and a full payload round-trips unchanged;
  * a notice can never be internally contradictory — no substitution without a
    replacement, no degradation that fails to say the floor was relaxed.

Hermetic throughout: `reputation_svc.fetch_reps` is the only thing on this
path that would reach the chain and it is always replaced with a fixed
snapshot, and the planning LLM is either stubbed or booby-trapped.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import ValidationError

from app.config import settings
from app.demo_kits import detect_kit
from app.schemas import Agent, DecomposeResponse, ExclusionReason, Plan, PlanFloorNotice, PlanStep, StoredPlan
from app.seed import seed_registry
from app.services import orchestrator_svc, reputation_svc
from app.services.reputation_svc import RepInfo
from app.state import state

# A curated intent, and one that must never match a kit. Which path each takes
# is decompose()'s business, not this file's — that is the point of the story.
KIT_INTENT = "tetris game in html"
FREE_FORM_INTENT = "write a launch announcement for a neighbourhood bakery"

# Sub-floor lower bound. Deliberately absurd rather than marginal so no floor
# an operator could plausibly configure lets it through, and so the number can
# be asserted on when it reappears as data on the notice.
SUB_FLOOR_LOWER_BPS = 100

# agt_02k2 (design.figma) holds "ui"/"tokens"/"figma", which no off-pipeline
# agent shares — so the kit path DROPS it rather than substituting, which is
# the one floor action the free-form path can also produce. That makes it the
# only agent the two paths can be compared on directly.
UNSUBSTITUTABLE_KIT_AGENT = "agt_02k2"


async def _noop(*_a: object, **_k: object) -> None:
    return None


def _rep(agent_id: str, *, smoothed: int, lower: int, degraded: bool = False) -> RepInfo:
    """An on-chain reputation entry with the two numbers routing actually reads."""
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
        degraded=degraded,
    )


def _sub_floor(agent_id: str, *, smoothed: int = 4000) -> RepInfo:
    """A rep entry that FAILS the routing floor on both paths."""
    return _rep(agent_id, smoothed=smoothed, lower=SUB_FLOOR_LOWER_BPS)


def _clears_floor(agent_id: str) -> RepInfo:
    """A rep entry comfortably above any shipped floor."""
    return _rep(agent_id, smoothed=8000, lower=8000)


@pytest.fixture()
def seeded(monkeypatch: pytest.MonkeyPatch) -> object:
    """Fresh 12-agent registry, restored after; kit thinking-sleep no-op'd.

    Mirrors tests/test_kit_floor.py rather than importing from it: a fixture
    reached across test modules is a fixture two lanes can break each other
    with, and this file must keep failing for story reasons only.
    """
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    monkeypatch.setattr(orchestrator_svc.asyncio, "sleep", _noop)
    yield
    state.agents.clear()
    state.agents.update(saved)


def _freeze_reps(monkeypatch: pytest.MonkeyPatch, reps: dict[str, RepInfo]) -> None:
    """Pin the reputation snapshot decompose() reads — and keep it offline."""

    async def _fake_reps(_ids: object, *_a: object, **_k: object) -> dict[str, RepInfo]:
        return reps

    monkeypatch.setattr(orchestrator_svc.reputation_svc, "fetch_reps", _fake_reps)


def _run_kit(monkeypatch: pytest.MonkeyPatch, reps: dict[str, RepInfo]) -> DecomposeResponse:
    """decompose() on the curated path, with the planning LLM booby-trapped.

    The kit path is deterministic by design; a call to the model here means
    the short circuit broke, and that must fail the test rather than quietly
    cost money on every demo.
    """

    async def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("the kit path must never call the LLM")

    _freeze_reps(monkeypatch, reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _boom)
    return asyncio.run(orchestrator_svc.decompose(KIT_INTENT))


def _run_free_form(
    monkeypatch: pytest.MonkeyPatch,
    reps: dict[str, RepInfo],
    picks: list[str],
) -> DecomposeResponse:
    """decompose() on the free-form path with the planner LLM stubbed.

    `picks` is what the model "returns". Nothing here inspects the prompt: the
    prompt is an implementation detail this story is actively rewriting, and a
    test that reads it would fail for reasons that are not the contract.
    """
    # Guard: a new kit trigger that happened to match this string would turn
    # the free-form half of every comparison below into a second kit run, and
    # the suite would go green while the story stayed unimplemented.
    assert detect_kit(FREE_FORM_INTENT) is None

    async def _arun(*_a: object, **_k: object) -> SimpleNamespace:
        steps = [
            PlanStep(
                agent_id=agent_id,
                rationale="model-chosen step",
                est_price_usdc=0.05,
                est_eta_seconds=1.0,
            )
            for agent_id in picks
        ]
        return SimpleNamespace(content=Plan(steps=steps))

    _freeze_reps(monkeypatch, reps)
    monkeypatch.setattr(orchestrator_svc.orchestrator_agent, "arun", _arun)
    return asyncio.run(orchestrator_svc.decompose(FREE_FORM_INTENT))


def _reported(resp: DecomposeResponse) -> list[tuple[str, str, str, int | None, int]]:
    """Every floor action on a response, as comparable data.

    Deliberately includes the deciding numbers: two paths that agree on the
    verdict but disagree on `lower_bound_bps` or `floor_bps` render two
    different sentences on the same plan card, which is the defect this story
    exists to remove.
    """
    return sorted((n.kind, n.agent_id, n.reason_code, n.lower_bound_bps, n.floor_bps) for n in resp.notices)


def test_both_paths_report_the_same_floor_action(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-6. One reputation snapshot, two intents, identical reporting.

    If this stops holding, the product has a trust gate that only narrates
    itself on demo intents: the free-form path drops a sub-floor agent from
    its candidate pool exactly as the kit path does, and a buyer reading the
    response cannot tell the agent was ever considered. Every downstream
    renderer — plan card, integration guide, dispute evidence — then has to
    special-case which intent produced the plan.
    """
    # One agent under the floor, everyone else silent (no entry == cold start
    # == routable), so each path has exactly one action to report and the two
    # responses can be compared element for element.
    reps = {UNSUBSTITUTABLE_KIT_AGENT: _sub_floor(UNSUBSTITUTABLE_KIT_AGENT)}

    kit = _run_kit(monkeypatch, reps)
    free_form = _run_free_form(monkeypatch, reps, ["agt_11c0", "agt_01h8"])

    # The agent really is gone from both plans — without this the comparison
    # below could be satisfied by two paths that both report nothing.
    assert UNSUBSTITUTABLE_KIT_AGENT not in [s.agent_id for s in kit.steps]
    assert UNSUBSTITUTABLE_KIT_AGENT not in [s.agent_id for s in free_form.steps]

    expected = [
        (
            "excluded",
            UNSUBSTITUTABLE_KIT_AGENT,
            "below_floor",
            SUB_FLOOR_LOWER_BPS,
            kit.floor_bps,
        )
    ]
    assert _reported(kit) == expected
    assert _reported(free_form) == expected

    # Same floor, same snapshot health: the two responses describe one world.
    assert kit.floor_bps == free_form.floor_bps
    assert kit.reputation_degraded == free_form.reputation_degraded

    # The prose differs (it names a different plan), but both paths owe the
    # buyer a sentence — a reason_code with no reason is an unrenderable card.
    for resp in (kit, free_form):
        assert all(n.reason.strip() for n in resp.notices)


def test_both_paths_report_the_configured_floor(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2. `floor_bps` is the floor this plan was built against, from settings.

    The threshold is per-deployment config. A client that hardcodes 5500 to
    render "4.10 against a 3.00 floor" narrates the wrong number the moment an
    operator tunes REPUTATION_FLOOR_BPS — silently, and in the one sentence
    the buyer uses to judge whether the exclusion was fair. Two different
    floors are exercised so a constant baked into the response cannot pass.
    """
    reps = {UNSUBSTITUTABLE_KIT_AGENT: _sub_floor(UNSUBSTITUTABLE_KIT_AGENT)}

    for floor in (4200, 3300):
        # Both below the shipped 5500 default, so a prior-only agent still
        # clears them and the only floor action stays the one this test set up.
        monkeypatch.setattr(settings, "reputation_floor_bps", floor)

        kit = _run_kit(monkeypatch, reps)
        free_form = _run_free_form(monkeypatch, reps, ["agt_11c0"])

        assert kit.floor_bps == floor
        assert free_form.floor_bps == floor

        # And the same number reaches the notice, so a card rendering one
        # exclusion never has to reach back to the envelope to find the floor.
        assert [n.floor_bps for n in kit.notices] == [floor]
        assert [n.floor_bps for n in free_form.notices] == [floor]


def test_unpicked_agents_are_not_reported_as_excluded(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3. Not being chosen is not an exclusion.

    The whole point of a notice is that it is rare enough to read. A registry
    of twelve agents produces a plan of one to six, so reporting "everyone we
    didn't hire" would put five to eleven lines on every single plan card and
    bury the one line that says an agent failed the trust gate. Product rules
    forbid it, and `not_selected_by_planner` is deliberately not in the reason
    vocabulary — this test is what stops it being reintroduced by behaviour
    instead of by a Literal.
    """
    # Every agent well clear of the floor: nothing here is a floor action.
    reps = {a.id: _clears_floor(a.id) for a in state.list_agents()}

    kit = _run_kit(monkeypatch, reps)
    free_form = _run_free_form(monkeypatch, reps, ["agt_11c0"])

    # Both plans genuinely left agents on the table — otherwise there is
    # nothing to under-report and the assertions below are vacuous.
    assert len(kit.steps) < len(state.agents)
    assert len(free_form.steps) == 1

    assert [n.agent_id for n in kit.notices] == []
    assert [n.agent_id for n in free_form.notices] == []

    # Named explicitly because agt_04m1 (sol-audit) is dispatchable, clears
    # the floor, and is on neither plan — the exact shape of agent a
    # list-everything implementation would report.
    for resp in (kit, free_form):
        assert "agt_04m1" not in [s.agent_id for s in resp.steps]
        assert not any(n.reason_code == "below_floor" for n in resp.notices)


def test_pre_3_02_payloads_still_validate() -> None:
    """AC-5. Every field this story adds is optional, at the model level.

    `DecomposeResponse` is not only a response shape — it is parsed back from
    stored and forwarded payloads, and the FE and the integration guide both
    build it. If any 3.02 field became required, every plan written before
    this deploy would stop validating on read, which is a data outage dressed
    up as a schema change.
    """
    # Exactly the fields that existed before 3.02, nothing more.
    notice = PlanFloorNotice(kind="excluded", agent_id="agt_02k2", reason="below routing floor")
    assert notice.reason_code == "below_floor"  # the only action the old code took
    assert notice.lower_bound_bps is None
    assert notice.floor_bps == 0

    resp = DecomposeResponse(
        plan_id="pln_0000",
        intent="write a launch announcement",
        steps=[],
        total_usdc=0.0,
        total_eta=0.0,
    )
    assert resp.notices == []
    assert resp.floor_bps == 0
    assert resp.reputation_degraded is False

    # The new keys are PRESENT in the serialized payload with their defaults.
    # A client reading `floor_bps` off an old plan gets 0, never a missing key.
    dumped = resp.model_dump()
    assert dumped["floor_bps"] == 0
    assert dumped["reputation_degraded"] is False
    assert dumped["notices"] == []


def test_full_payload_round_trips_unchanged() -> None:
    """AC-5. Dump and reparse is lossless, including every 3.02 field.

    The plan a buyer authorizes is serialized, stored, and read back before it
    is executed. A field that survives the response but not the round trip
    means the exclusion a buyer saw is not the exclusion the system later
    believes it showed them — and that is the record a dispute is judged on.
    """
    original = DecomposeResponse(
        plan_id="pln_beef",
        intent="tetris game in html",
        steps=[
            PlanStep(
                agent_id="agt_01h8",
                agent_name="copywrite.v3",
                rationale="stand in for the sub-floor brief agent",
                est_price_usdc=0.012,
                est_eta_seconds=0.5,
                rep_bps=7000,
                rep_source="onchain",
                substituted_for="agt_05x7",
                degraded=False,
            )
        ],
        total_usdc=0.012,
        total_eta=0.5,
        notices=[
            PlanFloorNotice(
                kind="substituted",
                agent_id="agt_05x7",
                agent_name="seo.brief",
                replacement_id="agt_01h8",
                replacement_name="copywrite.v3",
                reason="below routing floor (100 < 5500 bps)",
                reason_code="below_floor",
                lower_bound_bps=100,
                floor_bps=5500,
            ),
            PlanFloorNotice(
                kind="degraded",
                agent_id="agt_09l5",
                agent_name="research.pro",
                reason="re-admitted below the floor to keep the plan workable",
                reason_code="floor_relaxed",
                lower_bound_bps=4900,
                floor_bps=5500,
            ),
        ],
        floor_bps=5500,
        reputation_degraded=True,
    )

    assert DecomposeResponse.model_validate(original.model_dump()) == original


def test_exclusion_reason_vocabulary_is_closed() -> None:
    """The reason vocabulary is a fixed set of three, and two absences are load-bearing.

    Each value is rendered as one sentence on the plan card and documented in
    the integration guide, so an open vocabulary is an unrenderable and
    undocumentable card. Two values story 3.02 asked for are deliberately NOT
    here, and this test is the note that stops someone "completing" the set:

      * `inactive` — `AgentRegistry.set_active(id, false)` syncs through to
        `Agent.status == "offline"`, and routing does honour it
        (`orchestrator_svc._is_listed`): a delisted agent is never offered,
        kept, substituted in or re-admitted. It is still not a reason code,
        because a withdrawal is the operator's own decision rather than a
        verdict the floor reached, so a delisted agent gets no notice at all —
        tests/test_delisted_routing.py pins that silence.
      * `not_selected_by_planner` — forbidden by the story's own product
        rules, and by test_unpicked_agents_are_not_reported_as_excluded above:
        listing every unhired agent drowns the signal these notices exist to
        create.

    Adding either one is a contract change, not a fix.
    """
    assert get_args(ExclusionReason) == ("below_floor", "unbound_endpoint", "floor_relaxed")

    # And the model actually enforces it — a Literal that is never validated
    # against is a comment.
    for rejected in ("inactive", "not_selected_by_planner", ""):
        with pytest.raises(ValidationError):
            PlanFloorNotice(
                kind="excluded",
                agent_id="agt_02k2",
                reason="whatever the caller felt like",
                reason_code=rejected,  # type: ignore[arg-type]
            )


def _assert_notice_invariants(resp: DecomposeResponse, path: str) -> None:
    """The rules that make a notice renderable, checked on every notice.

    `kind` says what happened to the PLAN and `reason_code` says why; they are
    orthogonal, which is exactly why they can contradict each other. Each rule
    below is one sentence the plan card would otherwise have to render as
    nonsense.
    """
    for n in resp.notices:
        where = f"{path}: {n.kind}/{n.reason_code} for {n.agent_id}"

        if n.kind == "substituted":
            # "Replaced by nothing" is not a substitution — it is a drop that
            # forgot to say so, and the step it names is still in the plan.
            assert n.replacement_id, f"{where}: substitution with no replacement"

        if n.kind == "degraded":
            # A degradation IS the floor being relaxed. Leaving the default
            # `below_floor` here tells the buyer the agent was removed for
            # failing the gate while it is standing in their plan.
            assert n.reason_code == "floor_relaxed", f"{where}: degradation must say the floor was relaxed"

        if n.reason_code == "unbound_endpoint":
            # Nothing can substitute for or relax an endpoint that does not
            # exist; the only honest outcome is exclusion.
            assert n.kind == "excluded", f"{where}: an unbound endpoint can only be an exclusion"

        # One floor per plan. A notice quoting a different threshold from the
        # envelope it arrived in makes the card argue with itself.
        assert n.floor_bps == resp.floor_bps, f"{where}: notice floor {n.floor_bps} != plan floor {resp.floor_bps}"

        # bps of a 0..100 score. None is legal (the agent had no rep entry);
        # anything outside the scale is a unit mix-up reaching the buyer.
        assert n.lower_bound_bps is None or 0 <= n.lower_bound_bps <= 10_000, f"{where}: implausible lower bound"

        assert n.reason.strip(), f"{where}: notice with no reason"


def test_notices_are_internally_consistent_on_both_paths(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every notice either path emits has to hold together on its own terms.

    A plan card reads one notice at a time and renders it verbatim. A
    substitution with no replacement, or a re-admitted agent whose reason_code
    says it was excluded, produces a card that tells the buyer something the
    plan beside it contradicts — and that card is the record they authorize a
    payment against.
    """
    # The whole curated pipeline under the floor at once. On the kit path this
    # is the starvation scenario: one substitution, two re-admissions by the
    # backstop, three outright drops — the only snapshot that produces all
    # three `kind` values in one response. On the free-form path the same six
    # agents drop out of the candidate pool.
    scores = {
        "agt_09l5": 5000,
        "agt_05x7": 4600,
        "agt_02k2": 4000,
        "agt_11c0": 4900,
        "agt_12r0": 4800,
        "agt_08j2": 4700,
    }
    reps = {aid: _sub_floor(aid, smoothed=score) for aid, score in scores.items()}

    kit = _run_kit(monkeypatch, reps)
    # agt_01h8 is off the kit pipeline and keeps its cold-start score, so the
    # model names an agent that actually clears the floor.
    free_form = _run_free_form(monkeypatch, reps, ["agt_01h8"])

    # Non-empty on BOTH paths, or the loop below asserts nothing. Six agents
    # just failed the trust gate; a path that reports none of that is the
    # defect this story was opened for.
    assert kit.notices, "the kit path reported no floor action"
    _assert_notice_invariants(kit, "kit")

    assert free_form.notices, "the free-form path reported no floor action"
    _assert_notice_invariants(free_form, "free-form")


def test_both_paths_report_a_degraded_reputation_snapshot(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-6, the other uniform field: `reputation_degraded` on both paths.

    When the ledger is unreadable every read falls back to the Bayesian prior,
    which clears the shipped floor — so the floor fails OPEN and the plan
    still builds, with nobody excluded. That is deliberate, but it means the
    trust numbers on the card are an estimate rather than earned evidence, and
    the buyer is about to authorize payment against them. A flag that only
    appears on kit intents tells them so on the one plan they are least likely
    to pay for.
    """
    healthy = {a.id: _rep(a.id, smoothed=7000, lower=5677) for a in state.list_agents()}
    assert _run_kit(monkeypatch, healthy).reputation_degraded is False
    assert _run_free_form(monkeypatch, healthy, ["agt_11c0"]).reputation_degraded is False

    # Same scores, but every one of them is a prior standing in for a read
    # that failed — the shape fetch_reps returns during a Soroban outage.
    outage = {a.id: _rep(a.id, smoothed=7000, lower=5677, degraded=True) for a in state.list_agents()}
    kit = _run_kit(monkeypatch, outage)
    free_form = _run_free_form(monkeypatch, outage, ["agt_11c0"])

    assert kit.reputation_degraded is True
    assert free_form.reputation_degraded is True

    # Fail-open, not fail-empty: the outage must not cost the buyer a plan.
    assert kit.steps
    assert free_form.steps


def test_both_paths_report_unbound_agents_the_same_way(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-6 for the registry half of the vocabulary: `unbound_endpoint`.

    An indexed on-chain agent with no endpoint is marketplace-visible and
    un-routable. The free-form path told the buyer so; the kit path, which
    every demo takes, said nothing — so the same registry produced two
    different accounts of itself depending on the intent. Both paths now draw
    on one selection and put it in the same place: after the floor's own
    notices, ordered by id.
    """
    # Inserted out of id order, so the order below is the builder's and not
    # the registry's.
    for agent_id in ("ext_idx2", "ext_idx1"):
        state.add_agent(
            Agent(
                id=agent_id,
                name=f"{agent_id}.remote",
                skills=["remote"],
                price=0.02,
                rep=4.99,
                status="online",
                runs=0,
                source="onchain",
            )
        )
    reps = {UNSUBSTITUTABLE_KIT_AGENT: _sub_floor(UNSUBSTITUTABLE_KIT_AGENT)}

    kit = _run_kit(monkeypatch, reps)
    free_form = _run_free_form(monkeypatch, reps, ["agt_11c0"])

    floor = settings.reputation_floor_bps
    expected = [
        ("excluded", UNSUBSTITUTABLE_KIT_AGENT, "below_floor", SUB_FLOOR_LOWER_BPS, floor),
        ("excluded", "ext_idx1", "unbound_endpoint", None, floor),
        ("excluded", "ext_idx2", "unbound_endpoint", None, floor),
    ]
    for resp in (kit, free_form):
        # In emitted order, not sorted: the grouping is part of the contract.
        assert [(n.kind, n.agent_id, n.reason_code, n.lower_bound_bps, n.floor_bps) for n in resp.notices] == expected


def test_both_paths_stamp_the_same_reputation_on_a_step(seeded: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """The enriched step contract, identical on both paths.

    A plan card renders the numbers the floor was judged on — the lower bound,
    how many ratings stand behind it, how many were disputed, whether the read
    even succeeded — from the step itself. If one path stamps them and the
    other does not, the card for a free-form intent goes blank exactly where
    the demo intent showed the evidence.

    Two agents, one per question: code.gen with distinctive on-chain evidence,
    and code.critic whose read FAILED and was served the prior. The second is
    where `rep_degraded` and `degraded` must not be confused: the read failed,
    the agent still cleared the floor, and nothing was re-admitted.
    """
    reps = {a.id: _clears_floor(a.id) for a in state.list_agents()}
    reps["agt_11c0"] = RepInfo(
        agent_id="agt_11c0",
        smoothed_bps=8200,
        lower_bound_bps=7100,
        avg_bps=8400,
        count=42,
        weight=42 * 10_000_000,
        disputed=3,
        dispute_rate_bps=714,
        source="onchain",
    )
    reps["agt_12r0"] = reputation_svc._prior_info("agt_12r0", degraded=True)

    kit = _run_kit(monkeypatch, reps)
    free_form = _run_free_form(monkeypatch, reps, ["agt_11c0", "agt_12r0"])

    for agent_id in ("agt_11c0", "agt_12r0"):
        info = reps[agent_id]
        expected = (
            info.smoothed_bps,
            info.source,
            info.lower_bound_bps,
            info.count,
            info.dispute_rate_bps,
            info.degraded,
            False,
        )
        for resp in (kit, free_form):
            step = next(s for s in resp.steps if s.agent_id == agent_id)
            stamp = (
                step.rep_bps,
                step.rep_source,
                step.rep_lower_bound_bps,
                step.rep_count,
                step.rep_dispute_rate_bps,
                step.rep_degraded,
                step.degraded,
            )
            assert stamp == expected

    assert kit.reputation_degraded is True
    assert free_form.reputation_degraded is True


def test_plan_steps_without_the_enriched_fields_still_validate() -> None:
    """The four reputation fields on a step are additive, like the rest of 3.02.

    `PlanStep` is parsed back from stored plans and is also the planner's
    output schema, so a required field would reject every plan written before
    this deploy and every model answer that leaves it out — which is all of
    them, since the model is never asked for reputation.
    """
    step = PlanStep(agent_id="agt_11c0", rationale="build it", est_price_usdc=0.054, est_eta_seconds=2.6)
    assert (step.rep_lower_bound_bps, step.rep_count, step.rep_dispute_rate_bps, step.rep_degraded) == (
        None,
        None,
        None,
        False,
    )

    # And the populated shape survives storage, in memory and over the wire.
    full = step.model_copy(
        update={
            "rep_bps": 8200,
            "rep_source": "onchain",
            "rep_lower_bound_bps": 7100,
            "rep_count": 42,
            "rep_dispute_rate_bps": 714,
            "rep_degraded": True,
        }
    )
    stored = StoredPlan(id="pln_beef", intent="build it", plan=Plan(steps=[full]), total_usdc=0.054, total_eta=2.6)
    assert StoredPlan.model_validate(stored.model_dump()) == stored
    assert StoredPlan.model_validate_json(stored.model_dump_json()) == stored
