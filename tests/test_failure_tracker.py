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
