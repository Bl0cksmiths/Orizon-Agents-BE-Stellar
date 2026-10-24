"""When the refund reconcile sweep runs: at boot, when a claim taken is old
enough, on a short retry while a pass left something it can finish, and
otherwise only on the hourly fallback — so an idle deployment does not read
the dispute store every two minutes. Driven on a fake clock, no real sleeps."""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.services import refund_reconcile
from app.services.refund_reconcile import (
    CLAIM_WAKE_MARGIN_SECONDS,
    MIN_CLAIM_AGE_SECONDS,
    RETRY_SECONDS,
    SweepReport,
    next_run,
)

FINISHED = 10_000.0
CLAIM_DELAY = MIN_CLAIM_AGE_SECONDS + CLAIM_WAKE_MARGIN_SECONDS


class Clock:
    def __init__(self, now: float = FINISHED) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def fresh_schedule(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(refund_reconcile, "_now", clock)
    monkeypatch.setattr(refund_reconcile, "_wake", None)
    monkeypatch.setattr(refund_reconcile, "_due", None)
    monkeypatch.setattr(refund_reconcile, "_next_at", None)
    monkeypatch.setattr(refund_reconcile, "_next_reason", None)
    monkeypatch.setattr(settings, "refund_reconcile_interval_seconds", 3600.0)
    return clock


def _report(*, skipped: str | None = None, failed: bool = False, **outcomes: int) -> SweepReport:
    return SweepReport(FINISHED - 1, FINISHED, dict(outcomes), skipped=skipped, failed=failed)


# ── the next run, as a pure decision ──────────────────────────────────────


def test_a_pass_that_found_nothing_waits_for_the_fallback() -> None:
    assert next_run(_report(), None, finished_at=FINISHED) == (FINISHED + 3600, "fallback")


@pytest.mark.parametrize(
    "action", ["credited", "released", "already_settled", "no_hash", "history_gap", "not_this_refund", "lost_race"]
)
def test_a_pass_that_left_nothing_for_itself_waits_for_the_fallback(action: str) -> None:
    """A claim a human has to look at is not re-read every two minutes."""
    assert next_run(_report(**{action: 1}), None, finished_at=FINISHED) == (FINISHED + 3600, "fallback")


@pytest.mark.parametrize("action", ["young", "pending", "rpc_error", "error"])
def test_a_pass_that_left_something_it_can_finish_retries_soon(action: str) -> None:
    report = _report(credited=2, **{action: 1})
    assert next_run(report, None, finished_at=FINISHED) == (FINISHED + RETRY_SECONDS, "retry")


def test_a_pass_that_raised_or_could_not_read_the_store_retries_soon() -> None:
    assert next_run(None, None, finished_at=FINISHED) == (FINISHED + RETRY_SECONDS, "retry")
    failed = _report(skipped="ConnectionError", failed=True)
    assert next_run(failed, None, finished_at=FINISHED) == (FINISHED + RETRY_SECONDS, "retry")


def test_a_deployment_that_cannot_pay_credits_is_not_retried() -> None:
    gap = _report(skipped="no signing key")
    assert next_run(gap, None, finished_at=FINISHED) == (FINISHED + 3600, "fallback")


def test_a_claim_due_before_the_fallback_runs_the_pass_then() -> None:
    assert next_run(_report(), FINISHED + 315, finished_at=FINISHED) == (FINISHED + 315, "claim")
    # ...and a retry due first still goes first.
    assert next_run(_report(young=1), FINISHED + 315, finished_at=FINISHED) == (FINISHED + RETRY_SECONDS, "retry")


def test_a_fallback_shorter_than_the_retry_bounds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "refund_reconcile_interval_seconds", 60.0)
    assert next_run(None, None, finished_at=FINISHED) == (FINISHED + 60, "fallback")


# ── the signal ────────────────────────────────────────────────────────────


def test_a_claim_taken_is_due_once_it_is_old_enough_to_judge(fresh_schedule: Clock) -> None:
    refund_reconcile.claim_taken()
    assert refund_reconcile._due == FINISHED + CLAIM_DELAY
    fresh_schedule.now += 100
    refund_reconcile.claim_taken()  # a later claim never pushes the earlier one back
    assert refund_reconcile._due == FINISHED + CLAIM_DELAY


def test_a_claim_taken_wakes_a_waiting_sweep() -> None:
    async def go() -> bool:
        refund_reconcile._wake = asyncio.Event()
        refund_reconcile.claim_taken()
        return refund_reconcile._wake.is_set()

    assert asyncio.run(go()) is True


def test_a_claim_taken_without_a_sweep_running_is_harmless() -> None:
    refund_reconcile.claim_taken()  # no loop, no event: recorded, nothing raised


# ── the loop ──────────────────────────────────────────────────────────────


class _End(BaseException):
    """Stops the loop once the script has run out."""


def test_the_loop_runs_at_boot_then_on_a_claim_the_fallback_and_a_retry(
    monkeypatch: pytest.MonkeyPatch, fresh_schedule: Clock
) -> None:
    clock = fresh_schedule
    reports = [_report(), _report(credited=1), _report(rpc_error=1)]
    passes: list[tuple[float, str | None]] = []
    waits: list[float] = []
    # What happens during each wait: the time runs out, or a claim is taken.
    during = ["claim", "time", "time", "time"]

    async def sweep() -> SweepReport:
        passes.append((clock.now, refund_reconcile._next_reason))
        if not reports:
            raise _End
        return reports.pop(0)

    async def wait(timeout: float) -> bool:
        waits.append(timeout)
        if during.pop(0) == "claim":
            clock.now += 10
            refund_reconcile.claim_taken()
            return True
        clock.now += timeout
        return False

    monkeypatch.setattr(refund_reconcile, "sweep_once", sweep)
    monkeypatch.setattr(refund_reconcile, "_wait_for_wake", wait)

    with pytest.raises(_End):
        asyncio.run(refund_reconcile._loop())

    assert waits == [3600, CLAIM_DELAY, 3600, RETRY_SECONDS]
    assert passes == [
        (FINISHED, "boot"),
        (FINISHED + 10 + CLAIM_DELAY, "claim"),
        (FINISHED + 10 + CLAIM_DELAY + 3600, "fallback"),
        (FINISHED + 10 + CLAIM_DELAY + 3600 + RETRY_SECONDS, "retry"),
    ]
    assert refund_reconcile._due is None  # the claim's pass ran


def test_the_next_run_is_reported_while_the_sweep_waits(monkeypatch: pytest.MonkeyPatch, fresh_schedule: Clock) -> None:
    seen: list[dict[str, object]] = []

    async def sweep() -> SweepReport:
        return _report()

    async def wait(timeout: float) -> bool:
        seen.append(refund_reconcile.status())
        raise _End

    monkeypatch.setattr(refund_reconcile, "sweep_once", sweep)
    monkeypatch.setattr(refund_reconcile, "_wait_for_wake", wait)
    monkeypatch.setattr(refund_reconcile, "_task", None)

    async def go() -> None:
        refund_reconcile._task = asyncio.current_task()  # type: ignore[assignment]
        await refund_reconcile._loop()

    with pytest.raises(_End):
        asyncio.run(go())

    [status] = seen
    assert status["next_run_at"] == FINISHED + 3600
    assert status["next_run_reason"] == "fallback"
    refund_reconcile._task = None
    assert refund_reconcile.status()["next_run_at"] is None


def test_readiness_says_when_the_sweep_runs_next_and_why(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    async def waiting() -> None:
        refund_reconcile._next_at, refund_reconcile._next_reason = FINISHED + 3600, "fallback"
        await asyncio.sleep(3600)

    monkeypatch.setattr(refund_reconcile, "_loop", waiting)
    monkeypatch.setattr(settings, "refund_reconcile_enabled", True)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    with TestClient(app) as client:
        client.portal.call(asyncio.sleep, 0.01)
        during = client.get("/readiness").json()["disputes"]["reconcile"]
    after = refund_reconcile.status()

    assert (during["next_run_at"], during["next_run_reason"]) == (FINISHED + 3600, "fallback")
    assert (after["next_run_at"], after["next_run_reason"]) == (None, None)
