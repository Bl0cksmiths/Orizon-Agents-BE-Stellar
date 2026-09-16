"""Reputation-floor notice builders (story 3.02, BLO-24).

The point of `plan_notices` is that the demo-kit path and the free-form LLM
path cannot drift apart in how they report an exclusion, so these tests pin the
things a drift would show up in: the kind/reason_code pair each builder emits,
the exact prose a shipped frontend already reads, and where the floor number
comes from. The prose assertions are literal on purpose — comparing against
`orchestrator_svc`'s copy would pass happily on the day both copies changed
together and broke every client.

The floor is pinned to its default here rather than taken from config so the
literal sentences stay stable on a machine whose .env tunes it.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.schemas import Agent
from app.services import plan_notices
from app.services.reputation_svc import RepInfo

FLOOR = 5500


@pytest.fixture(autouse=True)
def pinned_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "reputation_floor_bps", FLOOR)


def _agent(agent_id: str, name: str | None = None) -> Agent:
    return Agent(
        id=agent_id,
        name=name or f"agent {agent_id}",
        skills=["code.gen"],
        price=0.1,
        rep=4.2,
        status="online",
        runs=7,
    )


def _info(agent_id: str, *, lower: int) -> RepInfo:
    """A reputation entry whose lower bound is exactly `lower`."""
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=lower + 500,
        lower_bound_bps=lower,
        avg_bps=lower,
        count=5,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


def test_below_floor_exclusion_reports_the_drop_and_the_deciding_numbers():
    n = plan_notices.below_floor_exclusion(_agent("agt_02k2"), _info("agt_02k2", lower=4200))

    assert (n.kind, n.reason_code) == ("excluded", "below_floor")
    assert n.agent_id == "agt_02k2"
    assert n.agent_name == "agent agt_02k2"
    assert n.reason == "below routing floor (4200 < 5500 bps)"
    assert n.lower_bound_bps == 4200
    assert n.floor_bps == FLOOR
    # Nothing stood in, so the replacement fields stay empty — a client keys
    # "was it replaced?" off these, not off the prose.
    assert n.replacement_id is None
    assert n.replacement_name is None


def test_unbound_exclusion_is_an_exclusion_for_a_different_reason():
    n = plan_notices.unbound_exclusion(_agent("ext_9"))

    # Same `kind` as a floor drop (the plan lost the agent either way), which is
    # exactly why the reason_code has to disagree.
    assert (n.kind, n.reason_code) == ("excluded", "unbound_endpoint")
    assert n.agent_id == "ext_9"
    assert n.reason == (
        "registered on-chain but no endpoint bound (nothing to dispatch a step to, so the planner passed it over)"
    )
    # Reputation had no part in this verdict, so there is no deciding bound.
    assert n.lower_bound_bps is None
    assert n.floor_bps == FLOOR


def test_the_unbound_reason_never_accuses_the_agent_of_failing():
    """An unbound agent is never dispatched to, so it has failed nothing.

    This codebase already corrected that wording once on the execution side
    (undispatched steps are not rated); a notice that reintroduced it would put
    the accusation back on the buyer's screen instead of the ledger.
    """
    reason = plan_notices.unbound_exclusion(_agent("ext_9")).reason

    assert "fail" not in reason
    assert "no endpoint bound" in reason


def test_substitution_names_both_agents_and_the_bound_that_lost_the_step():
    n = plan_notices.substitution(
        _agent("agt_02k2", "Design Tokens"),
        _agent("agt_77aa", "Stand-in"),
        _info("agt_02k2", lower=3100),
    )

    assert (n.kind, n.reason_code) == ("substituted", "below_floor")
    # The notice is about the agent the floor acted on, not the one now working.
    assert (n.agent_id, n.agent_name) == ("agt_02k2", "Design Tokens")
    assert (n.replacement_id, n.replacement_name) == ("agt_77aa", "Stand-in")
    assert n.reason == "below routing floor (3100 < 5500 bps)"
    assert n.lower_bound_bps == 3100
    assert n.floor_bps == FLOOR


def test_relaxation_is_degraded_and_quotes_the_callers_threshold():
    n = plan_notices.relaxation(_agent("agt_08j2"), _info("agt_08j2", lower=900), min_routable=3)

    assert (n.kind, n.reason_code) == ("degraded", "floor_relaxed")
    assert n.reason == "re-admitted below the floor to keep the plan workable (fewer than 3 agents cleared it)"
    # The agent is BELOW the floor and still in the plan; both numbers are on
    # the notice so the card can say so rather than imply it.
    assert n.lower_bound_bps == 900
    assert n.floor_bps == FLOOR


def test_relaxation_quotes_whatever_threshold_the_planner_used():
    """`min_routable` is the planner's number, not a constant in this module."""
    n = plan_notices.relaxation(_agent("agt_08j2"), None, min_routable=5)

    assert "fewer than 5 agents cleared it" in n.reason


def test_a_missing_rep_entry_reports_no_bound_rather_than_zero():
    """`None` and `0` are different facts and only one of them is true.

    An agent with no reputation entry PASSES the floor, so a 0 here would print
    the worst possible score next to an agent the planner was happy to route
    to. The historical prose still says 0; the structured field must not.
    """
    dropped = plan_notices.below_floor_exclusion(_agent("agt_01"), None)
    swapped = plan_notices.substitution(_agent("agt_01"), _agent("agt_02"), None)
    relaxed = plan_notices.relaxation(_agent("agt_01"), None, min_routable=3)

    assert dropped.lower_bound_bps is None
    assert swapped.lower_bound_bps is None
    assert relaxed.lower_bound_bps is None
    # The prose keeps its legacy placeholder — that is a compatibility surface.
    assert dropped.reason == "below routing floor (0 < 5500 bps)"


def test_a_zero_lower_bound_is_still_reported_as_zero():
    """The None-vs-0 rule must not swallow a genuine zero."""
    n = plan_notices.below_floor_exclusion(_agent("agt_01"), _info("agt_01", lower=0))

    assert n.lower_bound_bps == 0
    assert n.reason == "below routing floor (0 < 5500 bps)"


def test_every_builder_takes_the_floor_from_settings(monkeypatch: pytest.MonkeyPatch):
    """No builder accepts a floor argument, so a retuned floor lands everywhere.

    This is the property that keeps the two planning paths honest: neither can
    stamp a floor it cached earlier in the request, because neither is given
    one to cache.
    """
    monkeypatch.setattr(settings, "reputation_floor_bps", 8000)
    agent, info = _agent("agt_01"), _info("agt_01", lower=4200)

    notices = [
        plan_notices.below_floor_exclusion(agent, info),
        plan_notices.unbound_exclusion(agent),
        plan_notices.substitution(agent, _agent("agt_02"), info),
        plan_notices.relaxation(agent, info, min_routable=3),
    ]

    assert [n.floor_bps for n in notices] == [8000, 8000, 8000, 8000]
    # The prose follows the same setting, so the sentence and the field agree.
    assert notices[0].reason == "below routing floor (4200 < 8000 bps)"
    assert notices[2].reason == "below routing floor (4200 < 8000 bps)"


def test_unbound_exclusions_orders_by_agent_id():
    """Deterministic order, because the kit path is a reproducible demo.

    The caller's iterable comes from a registry walk, whose order is not a
    contract; the plan card's is.
    """
    shuffled = [_agent("ext_c"), _agent("ext_a"), _agent("ext_b")]

    ids = [n.agent_id for n in plan_notices.unbound_exclusions(shuffled)]

    assert ids == ["ext_a", "ext_b", "ext_c"]


def test_unbound_exclusions_caps_the_list():
    """The cap is the point: a permissionless registry has no bound on how many
    agents are unbound, and a roll-call of them buries the notices that matter."""
    many = [_agent(f"ext_{i:02d}") for i in range(40)]

    notices = plan_notices.unbound_exclusions(many)

    assert len(notices) == plan_notices.UNBOUND_REPORT_CAP
    assert all(n.reason_code == "unbound_endpoint" for n in notices)


def test_the_cap_applies_after_the_sort_so_the_same_names_survive():
    """Sorting after the cap would make the surviving eight depend on the order
    the caller happened to walk the registry in — two identical requests could
    then name different agents."""
    forwards = [_agent(f"ext_{i:02d}") for i in range(40)]
    backwards = list(reversed(forwards))

    assert [n.agent_id for n in plan_notices.unbound_exclusions(forwards)] == [
        n.agent_id for n in plan_notices.unbound_exclusions(backwards)
    ]
    assert [n.agent_id for n in plan_notices.unbound_exclusions(forwards)][0] == "ext_00"


def test_unbound_exclusions_of_nothing_is_nothing():
    """A plan where every agent was bound carries no notices at all — not an
    empty-ish placeholder the card would have to filter out."""
    assert plan_notices.unbound_exclusions([]) == []


def test_unbound_exclusions_builds_the_same_notice_as_the_single_builder():
    """One vocabulary, one construction site: the bulk helper is a cap and a
    sort over `unbound_exclusion`, never a second copy of the payload."""
    agent = _agent("ext_a")

    assert plan_notices.unbound_exclusions([agent]) == [plan_notices.unbound_exclusion(agent)]
