"""A config that excludes newcomers must say so at boot (story 3.06, AC-3).

Open registration and a reputation-gated router only coexist because the
floor is applied to the prior-smoothed lower bound, not to the raw on-chain
mean: a prior-only agent scores 5677 bps against a 5500 bps floor, so it is
routable, gets hired, gets rated, and earns its way onto real evidence.

Those 177 bps are arithmetic, not an invariant. Raise REPUTATION_FLOOR_BPS
above 5677, or lower REPUTATION_PRIOR_BPS or REPUTATION_PRIOR_WEIGHT_USDC,
and every agent registered from then on misses the floor on its first
request — so it is never routed, therefore never rated, therefore never able
to clear the floor. Nothing raises. Registration keeps returning 200 and the
marketplace simply stops hiring anyone new, which is a failure mode made
entirely of silence.

CI cannot catch it either: the suite runs against the repo defaults or a
test-time override, while this deployment takes its floor from the Render
dashboard, which overrides render.yaml. Only a check in the running process
sees the number actually in force — the startup line, and the `cold_start`
object /readiness reports on demand, which the last test holds to the same
verdict.

So these tests pin the check itself: that it runs on the real boot path,
that it names every number an operator needs to act on without opening
source, that it speaks on a healthy config too (a silent success is
indistinguishable from a check that did not run), and that it stays a
warning — a high floor is a policy an operator may intend, and refusing to
boot over a policy choice would take a live mainnet service down on merge.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.main import app
from app.services import reputation_svc as rep

LOGGER_NAME = "app.main"

# The shipped numbers, pinned here because the log line an operator reads is
# the deliverable — if these move, the message moves with them and the margin
# that makes registration real has changed.
SHIPPED_PRIOR_BPS = 7000
SHIPPED_PRIOR_WEIGHT_USDC = 12.0
SHIPPED_FLOOR_BPS = 5500
SHIPPED_PRIOR_BOUND_BPS = 5677
SHIPPED_MARGIN_BPS = 177


@pytest.fixture(autouse=True)
def shipped_reputation_config(monkeypatch):
    """Pin the reputation config to the repo defaults for every test here.

    The autouse hermetic fixture blanks contract ids but leaves these alone,
    so a developer's .env — or the Render-style override this story is about —
    would otherwise decide what the "healthy" cases assert.
    """
    monkeypatch.setattr(settings, "reputation_prior_bps", SHIPPED_PRIOR_BPS)
    monkeypatch.setattr(settings, "reputation_prior_weight_usdc", SHIPPED_PRIOR_WEIGHT_USDC)
    monkeypatch.setattr(settings, "reputation_floor_bps", SHIPPED_FLOOR_BPS)


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def _only_record(caplog) -> logging.LogRecord:
    records = _records(caplog)
    assert len(records) == 1, f"expected exactly one startup line, got {[r.getMessage() for r in records]}"
    return records[0]


def _report(caplog) -> logging.LogRecord:
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        main._report_cold_start_routability()
    return _only_record(caplog)


# ── the shipped config is healthy, and says so out loud ─────────


def test_the_shipped_config_reports_a_routable_cold_start(caplog):
    record = _report(caplog)
    assert record.levelno == logging.INFO
    assert "routable" in record.getMessage()


def test_a_healthy_config_still_says_something(caplog):
    """The whole point of the check. A line only on failure cannot be told
    apart from a check that was never wired into lifespan, and this exists
    because the failure it guards is itself silent."""
    assert _records(caplog) == []
    assert _report(caplog).levelno == logging.INFO


def test_the_healthy_line_names_every_number(caplog):
    """An operator must be able to read the margin off Render's log viewer
    and know how much room is left before the next floor bump locks the
    marketplace, without opening source to recompute it."""
    message = _report(caplog).getMessage()
    assert str(SHIPPED_PRIOR_BOUND_BPS) in message
    assert f"REPUTATION_FLOOR_BPS={SHIPPED_FLOOR_BPS}" in message
    assert f"REPUTATION_PRIOR_BPS={SHIPPED_PRIOR_BPS}" in message
    assert "REPUTATION_PRIOR_WEIGHT_USDC=12" in message
    assert f"{SHIPPED_MARGIN_BPS} bps of margin" in message


def test_the_healthy_line_is_one_line(caplog):
    """Boot emits it on every cold start of a free-tier instance; it does not
    get to be a paragraph."""
    assert "\n" not in _report(caplog).getMessage()


# ── a hostile floor is named, with the consequence ──────────────


def test_a_floor_above_the_prior_bound_warns(monkeypatch, caplog):
    monkeypatch.setattr(settings, "reputation_floor_bps", 6000)
    assert _report(caplog).levelno == logging.WARNING


def test_the_warning_names_every_number(monkeypatch, caplog):
    """A line reading "config may exclude new agents" is not actionable. The floor, the
    prior, its weight, the bound they produce and the shortfall between them
    are what an operator needs to decide which knob to move."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 6000)
    message = _report(caplog).getMessage()
    assert "REPUTATION_FLOOR_BPS=6000" in message
    assert f"{SHIPPED_PRIOR_BOUND_BPS} bps" in message
    assert f"REPUTATION_PRIOR_BPS={SHIPPED_PRIOR_BPS}" in message
    assert "REPUTATION_PRIOR_WEIGHT_USDC=12" in message
    assert f"{SHIPPED_PRIOR_BOUND_BPS - 6000} bps" in message  # the negative margin, -323


def test_the_warning_names_the_consequence(monkeypatch, caplog):
    """Not just "the floor is high": that a new agent can never be routed,
    and — the part that makes it permanent rather than slow — that never
    being routed is why it can never be rated out of the prior."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 6000)
    message = _report(caplog).getMessage()
    assert "never routed" in message
    assert "never rated" in message
    assert "never clear the floor" in message


def test_the_warning_names_the_remedy_and_where_the_value_comes_from(monkeypatch, caplog):
    """The floor in force is the Render dashboard's, not render.yaml's — an
    operator who fixes the file and redeploys would see the warning survive
    and have no idea why."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 6000)
    message = _report(caplog).getMessage()
    assert f"Lower REPUTATION_FLOOR_BPS to {SHIPPED_PRIOR_BOUND_BPS} or below" in message
    assert "Render dashboard" in message
    assert "render.yaml" in message


# ── the floor is not the only way to break it ───────────────────


def test_a_lowered_prior_breaks_the_guarantee_the_same_way(monkeypatch, caplog):
    """Nobody has to touch the floor. Dropping the prior mean sinks the bound
    under a floor that was never edited, and the marketplace closes to
    newcomers just as completely."""
    monkeypatch.setattr(settings, "reputation_prior_bps", 5000)
    record = _report(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "REPUTATION_PRIOR_BPS=5000" in message
    assert f"REPUTATION_FLOOR_BPS={SHIPPED_FLOOR_BPS}" in message
    assert "never routed" in message


def test_a_lowered_prior_weight_breaks_it_too(monkeypatch, caplog):
    """The least obvious of the three. Prior weight is evidence MASS, not a
    score, so trimming it looks like tuning how fast real ratings take over —
    but it widens the Wilson interval, drops the lower bound, and excludes
    every newcomer while the prior mean and the floor both read as before."""
    monkeypatch.setattr(settings, "reputation_prior_weight_usdc", 4.0)
    record = _report(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "REPUTATION_PRIOR_WEIGHT_USDC=4" in message
    assert f"REPUTATION_PRIOR_BPS={SHIPPED_PRIOR_BPS}" in message  # untouched, and still reported
    assert "4709 bps" in message  # the bound those 4 USDC of prior mass actually produce


# ── the boundary ────────────────────────────────────────────────


def test_a_floor_equal_to_the_prior_bound_still_clears(monkeypatch, caplog):
    """`passes_floor` is `>=`, so an agent sitting exactly on the floor is
    routed. The report must agree with the router to the bps, or it warns
    about a deployment that works — and a check that cries wolf on a healthy
    config is one operators learn to scroll past."""
    monkeypatch.setattr(settings, "reputation_floor_bps", SHIPPED_PRIOR_BOUND_BPS)
    record = _report(caplog)
    assert record.levelno == logging.INFO
    assert "0 bps of margin" in record.getMessage()
    assert rep.passes_floor(rep._prior_info("agt_new")) is True


def test_one_bps_above_the_bound_is_the_first_floor_that_excludes(monkeypatch, caplog):
    """The other side of the same edge, so the boundary is pinned from both
    directions rather than only where it is comfortable."""
    monkeypatch.setattr(settings, "reputation_floor_bps", SHIPPED_PRIOR_BOUND_BPS + 1)
    assert _report(caplog).levelno == logging.WARNING
    assert rep.passes_floor(rep._prior_info("agt_new")) is False


# ── it runs on the real boot path, and never refuses the boot ───


def test_the_check_runs_at_startup_not_merely_on_demand(caplog):
    """Called from lifespan, which is the only thing that makes it a startup
    guarantee — a helper nothing invokes is the same silence in a new shape."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), TestClient(app):
        pass
    record = _only_record(caplog)
    assert record.levelno == logging.INFO
    assert str(SHIPPED_PRIOR_BOUND_BPS) in record.getMessage()


def test_a_hostile_floor_warns_through_the_real_startup_path(monkeypatch, caplog):
    monkeypatch.setattr(settings, "reputation_floor_bps", 9000)
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), TestClient(app):
        pass
    record = _only_record(caplog)
    assert record.levelno == logging.WARNING
    assert "REPUTATION_FLOOR_BPS=9000" in record.getMessage()


def test_a_hostile_floor_still_boots_and_serves(monkeypatch, caplog):
    """A warning, not a ValueError. story 3.03's config validators refuse to
    boot because the configs they catch are always wrong; a floor above the
    prior bound is a policy an operator may mean (a curated network hiring
    only rated agents). Refusing the boot would turn that choice into an
    outage of a live mainnet deployment on the next autodeploy."""
    monkeypatch.setattr(settings, "reputation_floor_bps", 9999)
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert _only_record(caplog).levelno == logging.WARNING


# ── the log site reads numbers, it does not recompute them ──────


def test_the_margin_is_reported_as_data(caplog):
    """The log site formats what the service computed. Anything that had to
    re-derive the bound to print it would be a second copy of the arithmetic,
    free to drift from the one the router actually gates on."""
    margin = rep.cold_start_margin()
    assert margin.floor_bps == SHIPPED_FLOOR_BPS
    assert margin.prior_bps == SHIPPED_PRIOR_BPS
    assert margin.prior_weight_usdc == SHIPPED_PRIOR_WEIGHT_USDC
    assert margin.lower_bound_bps == SHIPPED_PRIOR_BOUND_BPS
    assert margin.margin_bps == SHIPPED_MARGIN_BPS
    assert margin.clears is True
    # And it is the router's own number, not a parallel estimate of it.
    assert margin.lower_bound_bps == rep._prior_info("agt_new").lower_bound_bps


def test_the_reported_verdict_and_the_routing_verdict_cannot_disagree(monkeypatch):
    """`clears` is the claim the startup line makes about a newcomer;
    `passes_floor` is what the planner will actually do with one. They are
    asserted equal across the interesting floors, including both sides of the
    boundary, because a report that can differ from the behaviour it
    describes is worse than no report."""
    for floor in (0, 1000, SHIPPED_PRIOR_BOUND_BPS - 1, SHIPPED_PRIOR_BOUND_BPS, SHIPPED_PRIOR_BOUND_BPS + 1, 10_000):
        monkeypatch.setattr(settings, "reputation_floor_bps", floor)
        assert rep.cold_start_margin().clears is rep.passes_floor(rep._prior_info("agt_new"))
        assert rep.cold_start_margin().clears is rep.prior_clears_floor()


# ── the probe answers what the boot line said ───────────────────


def test_the_readiness_probe_and_the_startup_line_cannot_disagree(monkeypatch, caplog):
    """A free-tier instance restarts on every wake, so the boot line is often
    gone by the time anyone asks; /readiness answers the same question on
    demand. Two surfaces for one verdict are only safe while they are the same
    verdict, so they are compared through the real boot path on both sides of
    the boundary, with the service's own margin as the referee."""
    for floor in (SHIPPED_FLOOR_BPS, SHIPPED_PRIOR_BOUND_BPS, SHIPPED_PRIOR_BOUND_BPS + 1, 9000):
        monkeypatch.setattr(settings, "reputation_floor_bps", floor)
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME), TestClient(app) as client:
            cold_start = client.get("/readiness").json()["cold_start"]
        margin = rep.cold_start_margin()
        assert cold_start == {
            "routable": margin.clears,
            "lower_bound_bps": margin.lower_bound_bps,
            "floor_bps": floor,
            "margin_bps": margin.margin_bps,
        }
        assert cold_start["routable"] is (_only_record(caplog).levelno == logging.INFO)
