"""The last-known reachability of each bound endpoint (D-084).

The planner used to route to an agent its own readiness probe had just
reported unreachable: the buyer signed an authorize, the step failed in 0.3 s,
and the buyer paid two transactions' fees for a run the platform already knew
could not be delivered. `reachability` is the memory that closes that gap — the
verdict of the latest probe, per agent — and these tests pin its rules:

  * only a probe that ran and FAILED excludes, and only while it is fresh: a
    transient blip must not keep an agent out of every plan forever;
  * a later success, or an unbind, clears the failure at once;
  * an `unknown` probe (the check itself could not run) changes nothing,
    because it is not a verdict on the agent;
  * the map is bounded, because agent ids are caller-influenced.
"""

from __future__ import annotations

import pytest

from app.services import reachability


@pytest.fixture(autouse=True)
def clean_store(monkeypatch: pytest.MonkeyPatch):
    reachability.reset()
    clock = {"now": 1_000.0}
    monkeypatch.setattr(reachability, "_now", lambda: clock["now"])
    yield clock
    reachability.reset()


def test_a_failed_probe_excludes_the_agent(clean_store) -> None:
    reachability.record("ext_dead", "failed")

    assert reachability.is_failing("ext_dead")


def test_an_agent_never_probed_is_not_failing(clean_store) -> None:
    # No verdict is not a failure: the planner must not exclude an agent on
    # the strength of a check nobody ran.
    assert not reachability.is_failing("ext_never")


def test_a_failure_expires_after_the_freshness_window(clean_store) -> None:
    reachability.record("ext_blip", "failed")

    clean_store["now"] += reachability.FAILURE_FRESH_SECONDS - 1
    assert reachability.is_failing("ext_blip")

    clean_store["now"] += 2
    assert not reachability.is_failing("ext_blip"), "a transient blip must not exclude forever"


def test_a_later_success_clears_the_failure(clean_store) -> None:
    reachability.record("ext_back", "failed")
    reachability.record("ext_back", "done")

    assert not reachability.is_failing("ext_back")


def test_an_unknown_probe_changes_nothing(clean_store) -> None:
    # `unknown` means the check could not run (binding unreadable, probe
    # overran its bound). It says nothing about the agent either way.
    reachability.record("ext_dead", "failed")
    reachability.record("ext_dead", "unknown")
    assert reachability.is_failing("ext_dead")

    reachability.record("ext_up", "done")
    reachability.record("ext_up", "unknown")
    assert reachability.has_fresh_verdict("ext_up")


def test_nothing_bound_forgets_the_verdict(clean_store) -> None:
    # `todo` is "nothing is bound": the failure was about an endpoint that is
    # no longer there, so it must not outlive it into the next binding.
    reachability.record("ext_rebound", "failed")
    reachability.record("ext_rebound", "todo")

    assert not reachability.is_failing("ext_rebound")
    assert not reachability.has_fresh_verdict("ext_rebound")


def test_a_success_is_fresh_for_its_own_window_then_stale(clean_store) -> None:
    reachability.record("ext_up", "done")
    assert reachability.has_fresh_verdict("ext_up")

    clean_store["now"] += reachability.SUCCESS_FRESH_SECONDS + 1
    assert not reachability.has_fresh_verdict("ext_up")


def test_ids_outside_the_agent_id_shape_are_ignored(clean_store) -> None:
    reachability.record("not an id\n", "failed")

    assert not reachability.is_failing("not an id\n")
    assert reachability.tracked() == 0


def test_the_map_is_bounded(clean_store) -> None:
    for i in range(reachability.MAX_TRACKED + 10):
        reachability.record(f"ext_{i}", "failed")

    assert reachability.tracked() == reachability.MAX_TRACKED
    # Oldest out first: the newest verdicts are the ones a planner can use.
    assert not reachability.is_failing("ext_0")
    assert reachability.is_failing(f"ext_{reachability.MAX_TRACKED + 9}")
