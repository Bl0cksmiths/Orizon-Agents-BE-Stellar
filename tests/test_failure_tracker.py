"""Per-agent failure tracking (story 2.03, ADR 0005).

What is pinned here is a LOG CONTRACT as much as a counter: the module's value
is that an operator reading Render's log sees one line when an agent starts
failing, one more when the way it fails changes, one when it looks permanently
broken, and one when it comes back — and nothing else, however long the outage
runs. So the assertions are about levels and line COUNTS, not merely about
something having been logged; a version that warned on every failed step would
pass a "did it log?" test and still be the log flood this module exists to
prevent.

The load-bearing properties, in order:

  - streaks are per agent and reset on a success;
  - the map is bounded, evicts the least recently failing agent first, and
    says so;
  - WARNING on the first failure, on a change of failure class, and once on
    crossing the run-length threshold; DEBUG for the same class repeating;
  - exactly one INFO on recovery;
  - nothing operator-controlled — no URL, no host, no free text, no
    out-of-pattern agent id — can reach a log line through either argument.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.services import failure_tracker as ft
from app.services.failure_tracker import consecutive_failures, record_failure, record_success

LOGGER_NAME = "app.services.failure_tracker"


@pytest.fixture(autouse=True)
def clean_tracker():
    """The streak map and the at-capacity flag are process-global — a test that
    left either set would silently disarm the next one (the coalescing guards
    are exactly the state that makes a second WARNING not fire)."""
    ft._streaks.clear()
    ft._at_capacity = False
    yield
    ft._streaks.clear()
    ft._at_capacity = False


def _records(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME and r.levelno == level]


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME]


# ── counting ────────────────────────────────────────────────────


def test_an_agent_that_never_failed_has_no_streak():
    assert consecutive_failures("ext_translate") == 0


def test_failures_accumulate_per_agent_independently():
    record_failure("ext_alpha", "connect_failed")
    record_failure("ext_alpha", "connect_failed")
    record_failure("ext_beta", "http_status")

    assert consecutive_failures("ext_alpha") == 2
    assert consecutive_failures("ext_beta") == 1


def test_a_changed_class_extends_the_streak_rather_than_restarting_it():
    # A different way of failing is still failing: the counter answers "how
    # long has this been broken", not "how long in this particular way".
    record_failure("ext_alpha", "connect_failed")
    record_failure("ext_alpha", "http_status")
    record_failure("ext_alpha", "schema_rejected")

    assert consecutive_failures("ext_alpha") == 3


def test_success_resets_the_streak_for_that_agent_only():
    record_failure("ext_alpha", "connect_failed")
    record_failure("ext_alpha", "connect_failed")
    record_failure("ext_beta", "connect_failed")

    record_success("ext_alpha")

    assert consecutive_failures("ext_alpha") == 0
    assert consecutive_failures("ext_beta") == 1


def test_a_success_allocates_nothing():
    # The common path must not put the agent in the bounded map: tracking every
    # agent that ever ran would make the cap a function of traffic instead of
    # a function of how many agents are actually broken.
    record_success("ext_alpha")
    record_success("ext_beta")

    assert ft._streaks == {}


def test_a_streak_resumes_from_one_after_a_success():
    record_failure("ext_alpha", "connect_failed")
    record_failure("ext_alpha", "connect_failed")
    record_success("ext_alpha")
    record_failure("ext_alpha", "connect_failed")

    assert consecutive_failures("ext_alpha") == 1


def test_reading_the_counter_never_mutates_it(caplog):
    record_failure("ext_alpha", "connect_failed")
    caplog.clear()  # drop the setup's first-failure WARNING; only the reads matter here

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert consecutive_failures("ext_alpha") == 1
        assert consecutive_failures("ext_alpha") == 1
        assert consecutive_failures("ext_unknown") == 0

    assert list(ft._streaks) == ["ext_alpha"]
    assert _messages(caplog) == []  # a query is not an event


# ── escalation: what is worth a WARNING, and what is not ────────


def test_the_first_failure_warns_and_the_same_class_repeating_is_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for _ in range(4):
            record_failure("ext_alpha", "connect_failed")

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1  # not one per failed step — that is the flood
    assert "ext_alpha" in warnings[0].getMessage()
    assert "connect_failed" in warnings[0].getMessage()
    assert len(_records(caplog, logging.DEBUG)) == 3


def test_a_new_failure_class_warns_again_and_names_the_transition(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        record_failure("ext_alpha", "connect_failed")
        record_failure("ext_alpha", "connect_failed")
        record_failure("ext_alpha", "http_status")

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 2
    changed = warnings[1].getMessage()
    # Both ends of the transition, because "it changed" without saying from
    # what is not actionable — the operator is deciding whether their fix
    # moved the failure or their endpoint got worse.
    assert "connect_failed" in changed and "http_status" in changed
    assert "3 consecutive" in changed


def test_an_alternating_endpoint_warns_once_per_class_not_once_per_step(caplog):
    # The realistic flaky case: a struggling endpoint that refuses a connection
    # on one step and times out on the next changes class every single step.
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for rule in ["connect_failed", "read_timeout"] * 3:
            record_failure("ext_alpha", rule)

    assert len(_records(caplog, logging.WARNING)) == 2  # one per class, then silence
    assert len(_records(caplog, logging.DEBUG)) == 4


def test_a_long_streak_escalates_once_then_falls_back_to_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for _ in range(ft._ESCALATION_STREAK + 3):
            record_failure("ext_alpha", "connect_failed")

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 2  # the first failure, then the run-length crossing
    escalation = warnings[1].getMessage()
    assert f"{ft._ESCALATION_STREAK} consecutive steps" in escalation
    assert "persistently broken" in escalation


def test_the_escalation_threshold_outlives_a_single_plan():
    # The orchestrator decomposes an intent into 1–6 steps, so a threshold of 6
    # or less would report a single unlucky run — one plan that happened to
    # route every step at the same agent — as a persistently broken endpoint.
    assert ft._ESCALATION_STREAK > 6


def test_every_agent_gets_its_own_first_failure_warning(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        record_failure("ext_alpha", "connect_failed")
        record_failure("ext_beta", "connect_failed")

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert [w.getMessage().split()[1] for w in warnings] == ["ext_alpha", "ext_beta"]


# ── recovery ────────────────────────────────────────────────────


def test_recovery_logs_exactly_one_info_carrying_the_streak_it_ended(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for _ in range(3):
            record_failure("ext_alpha", "connect_failed")
        record_success("ext_alpha")

    infos = _records(caplog, logging.INFO)
    assert len(infos) == 1
    recovered = infos[0].getMessage()
    assert "ext_alpha" in recovered
    # The length and the last class are what make the line worth reading: they
    # say how bad it was and what it was doing, without a second grep.
    assert "3 consecutive" in recovered
    assert "connect_failed" in recovered


def test_a_success_for_a_healthy_agent_says_nothing(caplog):
    # Every successful step calls this. An INFO here would be one line per
    # step per agent forever — a worse flood than the one being fixed.
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for _ in range(5):
            record_success("ext_alpha")

    assert _messages(caplog) == []


def test_recovery_re_arms_the_warning_so_a_flapping_agent_stays_visible(caplog):
    # registry_sync's discipline: coalescing must not swallow the SECOND
    # outage. An agent that fails, recovers, and fails again is two incidents.
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        record_failure("ext_alpha", "connect_failed")
        record_success("ext_alpha")
        record_failure("ext_alpha", "connect_failed")

    assert len(_records(caplog, logging.WARNING)) == 2
    assert len(_records(caplog, logging.INFO)) == 1


def test_a_class_reported_before_a_recovery_is_reported_again_after_it(caplog):
    # `seen` dies with the streak: the same class in a NEW outage is news
    # again, otherwise a long-lived process would go quiet about a recurring
    # failure mode it had already reported once hours earlier.
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        record_failure("ext_alpha", "connect_failed")
        record_failure("ext_alpha", "http_status")
        record_success("ext_alpha")
        record_failure("ext_alpha", "http_status")

    assert len(_records(caplog, logging.WARNING)) == 3


# ── bounded: the cap, the eviction order, and the line it writes ─


@pytest.fixture()
def tiny_cap(monkeypatch):
    """Three slots, so eviction is exercised in four lines instead of 257.

    The cap is read live inside record_failure, so patching the module global
    is enough — nothing captures it at import.
    """
    monkeypatch.setattr(ft, "_MAX_AGENTS", 3)
    return 3


def _evictions(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in _records(caplog, level) if "failure tracker" in r.getMessage()]


def test_the_map_never_grows_past_the_cap(tiny_cap):
    for n in range(20):
        record_failure(f"ext_{n}", "connect_failed")

    assert len(ft._streaks) == tiny_cap


def test_the_oldest_agent_is_evicted_first(tiny_cap):
    for name in ["ext_a", "ext_b", "ext_c"]:
        record_failure(name, "connect_failed")

    record_failure("ext_d", "connect_failed")

    assert "ext_a" not in ft._streaks
    assert list(ft._streaks) == ["ext_b", "ext_c", "ext_d"]


def test_an_agent_that_is_still_failing_outlives_a_quiet_one(tiny_cap):
    # "Oldest" means least recently failing, not first seen: evicting the agent
    # that is failing RIGHT NOW in favour of one that has been quiet since
    # startup would drop the only streak anybody is going to grep for.
    for name in ["ext_a", "ext_b", "ext_c"]:
        record_failure(name, "connect_failed")
    record_failure("ext_a", "connect_failed")

    record_failure("ext_d", "connect_failed")

    assert list(ft._streaks) == ["ext_c", "ext_a", "ext_d"]


def test_an_evicted_streak_restarts_from_zero(tiny_cap):
    record_failure("ext_a", "connect_failed")
    record_failure("ext_a", "connect_failed")
    for name in ["ext_b", "ext_c", "ext_d"]:
        record_failure(name, "connect_failed")

    assert consecutive_failures("ext_a") == 0


def test_the_first_eviction_warns_and_names_what_was_dropped(tiny_cap, caplog):
    for name in ["ext_a", "ext_b", "ext_c"]:
        record_failure(name, "connect_failed")
        record_failure(name, "connect_failed")
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        record_failure("ext_d", "http_status")

    warnings = _evictions(caplog, logging.WARNING)
    assert len(warnings) == 1
    dropped = warnings[0].getMessage()
    assert "ext_a" in dropped
    # The streak that was lost, so the operator can tell a dropped counter from
    # an agent that genuinely just started failing.
    assert "connect_failed" in dropped and "2 consecutive" in dropped


def test_evictions_coalesce_to_debug_while_the_map_stays_full(tiny_cap, caplog):
    for name in ["ext_a", "ext_b", "ext_c"]:
        record_failure(name, "connect_failed")
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for n in range(5):
            record_failure(f"ext_new_{n}", "connect_failed")

    # At capacity every new agent evicts one, so an uncoalesced warning would
    # be a flood proportional to the churn — exactly what this module prevents.
    assert len(_evictions(caplog, logging.WARNING)) == 1
    assert len(_evictions(caplog, logging.DEBUG)) == 4


def test_a_success_re_arms_the_eviction_warning(tiny_cap, caplog):
    for name in ["ext_a", "ext_b", "ext_c", "ext_d"]:
        record_failure(name, "connect_failed")  # the fourth evicts and warns
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        record_success("ext_b")  # a slot is free again
        record_failure("ext_e", "connect_failed")  # fits, evicts nothing
        record_failure("ext_f", "connect_failed")  # full again: news, not churn

    assert len(_evictions(caplog, logging.WARNING)) == 1


# ── nothing operator-controlled reaches a log line ──────────────

# Values an operator (or an attacker who registered an agent) could plausibly
# steer into either argument. ADR 0003's rule is that a refusal logs the host
# and the rule, never the attacker-controlled URL; this module is one layer
# further in and logs neither.
POISONED_RULES = [
    "https://ops.example.com/orizon/dispatch?token=SUPERSECRET",
    "ConnectionRefusedError: [Errno 111] connecting to 10.0.0.5:8080",
    "connect_failed\nWARNING forged log line",
    "Connect_Failed",  # the vocabulary is lowercase — near-misses are not waved through
    "A" * 5000,
]

POISON_FRAGMENTS = ["SUPERSECRET", "ops.example.com", "10.0.0.5", "forged", "Errno"]


def test_a_free_text_rule_is_normalised_before_it_is_stored_or_logged(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for rule in POISONED_RULES:
            record_failure("ext_alpha", rule)

    assert ft._streaks["ext_alpha"].rule == "unclassified"  # normalised at ingest, not at print time
    messages = _messages(caplog)
    assert messages  # the failures were still counted and reported
    for message in messages:
        assert not any(fragment in message for fragment in POISON_FRAGMENTS)
        assert len(message) < 300  # the 5 000-character rule reached nothing


def test_varying_the_rule_cannot_force_a_warning_on_every_failure(caplog):
    # Every unusable value collapses to ONE token, so a caller who can vary the
    # rule freely still cannot manufacture a class change per step and turn the
    # coalescing guard into the log flood it exists to prevent.
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for n in range(10):
            record_failure("ext_alpha", f"failed at attempt {n} — see https://host{n}.example.com")

    warnings = _records(caplog, logging.WARNING)
    # Two, and neither is caller-steerable: the first failure, and the
    # run-length crossing at ten. Ten distinct values produced no class change.
    assert len(warnings) == 2
    assert all("unclassified" in w.getMessage() for w in warnings)
    assert consecutive_failures("ext_alpha") == 10  # still counted exactly


def test_a_vocabulary_token_survives_verbatim(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        record_failure("ext_alpha", "schema_rejected")

    assert ft._streaks["ext_alpha"].rule == "schema_rejected"
    assert "schema_rejected" in _messages(caplog)[0]


def test_an_agent_id_outside_the_pattern_is_neither_tracked_nor_logged(caplog):
    poisoned_ids = [
        "https://ops.example.com/orizon/dispatch?token=SUPERSECRET",
        "ext_alpha\nWARNING forged log line",
        "ext alpha",
        "A" * 33,  # AGENT_ID_PATTERN caps at 32
        "",
    ]

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for agent_id in poisoned_ids:
            record_failure(agent_id, "connect_failed")
            record_success(agent_id)
            assert consecutive_failures(agent_id) == 0

    assert ft._streaks == {}  # no slot in the bounded map for an id that cannot name an agent
    for record in caplog.records:
        if record.name != LOGGER_NAME:
            continue
        assert record.levelno == logging.DEBUG
        assert not record.args  # nothing interpolated at all — the value is withheld, not truncated
        assert not any(fragment in record.getMessage() for fragment in POISON_FRAGMENTS)


# ── concurrency ─────────────────────────────────────────────────


def test_concurrent_workflows_keep_every_counter_exact():
    """Up to `orchestrator_max_concurrent` (8) workflows run at once, all on
    one event loop under `--workers 1`.

    No lock is taken, and this is what makes that safe: the tracker's functions
    are synchronous and contain no `await`, so the loop cannot suspend halfway
    through a read-modify-write and hand the map to another workflow. The
    workflows below interleave as hard as asyncio allows — a suspension point
    between every call — and still land on exact counts. A version that
    awaited anything mid-update, or that ran off the loop in a worker thread,
    would lose increments here.
    """
    agents = [f"ext_{n}" for n in range(8)]
    steps = 6

    async def workflow(agent_id: str) -> None:
        for _ in range(steps):
            record_failure(agent_id, "connect_failed")
            await asyncio.sleep(0)
            record_failure("ext_shared", "http_status")
            await asyncio.sleep(0)

    async def run_all() -> None:
        await asyncio.gather(*(workflow(agent_id) for agent_id in agents))

    asyncio.run(run_all())

    assert [consecutive_failures(agent_id) for agent_id in agents] == [steps] * len(agents)
    assert consecutive_failures("ext_shared") == steps * len(agents)
