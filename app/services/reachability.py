"""The last-known reachability of each bound endpoint — what the planner reads (D-084).

The readiness self-check (`operator_readiness`) already probes a bound
endpoint and answers `reachable: failed` with a reason an operator can act on.
Nothing on the planning path read that answer, so the planner kept routing paid
work to an endpoint the platform itself had just found dead: the buyer signed
an authorize, the step failed in well under a second, and the buyer paid the
fees of a run that could not be delivered.

This module is the memory between the two: the verdict of the latest probe per
agent, recorded wherever a probe runs and read by the planner, which leaves an
agent out of a plan while a FRESH failure stands against it. The rules:

  * Only `failed` excludes. `unknown` (the check itself could not run) is not a
    verdict on the agent and changes nothing; `todo` (nothing bound) forgets
    whatever was known, because that verdict was about an endpoint that is no
    longer there.
  * A failure is fresh for `FAILURE_FRESH_SECONDS`. A transient blip — a host
    restarting, a free tier waking up — must not keep an agent out of every
    plan forever. Past the window the agent is offered again and re-probed in
    the background (`refresh_stale`), so a still-dead endpoint is caught again
    before it can collect many plans, and a recovered one is back without
    anyone having to ask.
  * A later success clears a failure at once.
  * Bounded: agent ids are caller-influenced (registration is permissionless,
    binding open to any registrant), so the map has a hard cap with
    oldest-first eviction, like `failure_tracker` and `app/stellar/cache.py`.

Single-process and in-memory, by the same argument as `failure_tracker`: the
service runs one worker (render.yaml), every function here is synchronous with
no `await`, so a read-modify-write cannot interleave. Nothing here survives a
restart, which is the right failure mode: after a deploy every bound agent is
unknown, offered, and re-probed on first use.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from ..schemas import AGENT_ID_PATTERN

logger = logging.getLogger(__name__)

# How long a failed probe keeps an agent out of plans. Five minutes: long
# enough that a dead endpoint is not re-offered (and re-sold) on the next
# request, short enough that an operator who fixed it is routable again within
# one coffee — and every expiry triggers a re-probe, so a fix is noticed even
# when nobody runs the readiness check.
FAILURE_FRESH_SECONDS = 300.0

# How long a success counts as known. Only decides when the planner asks again
# in the background; a success never excludes anything.
SUCCESS_FRESH_SECONDS = 300.0

# Hard cap on tracked agents. A few tens of KB at capacity.
MAX_TRACKED = 512

# Background probes running at once, across every plan. Each is one bounded GET
# (`operator_readiness.PROBE_TIMEOUT_SECONDS`), so this caps the outbound
# requests the planner can cause; agents past the cap are asked on a later plan.
MAX_IN_FLIGHT = 8

_ID_SHAPE = re.compile(AGENT_ID_PATTERN)


@dataclass(frozen=True)
class Verdict:
    reachable: bool
    at: float  # `_now()` when it was recorded


_verdicts: OrderedDict[str, Verdict] = OrderedDict()
_in_flight: dict[str, asyncio.Task[None]] = {}


def _now() -> float:
    """The clock verdicts are aged on. A seam for the tests."""
    return time.monotonic()


def record(agent_id: str, status: str) -> None:
    """Record a readiness `reachable` step's status for `agent_id`.

    `status` is the step's own vocabulary: `done`, `failed`, `todo` or
    `unknown`. Anything else is ignored, as is an id outside the agent-id
    shape, so nothing unbounded or unexpected is ever stored.
    """
    if not _ID_SHAPE.fullmatch(agent_id):
        return
    if status == "todo":
        _verdicts.pop(agent_id, None)
        return
    if status not in ("done", "failed"):
        return
    reachable = status == "done"
    previous = _verdicts.pop(agent_id, None)
    if previous is not None and previous.reachable != reachable:
        logger.info("reachability: agent_id=%s reachable=%s (was %s)", agent_id, reachable, previous.reachable)
    elif previous is None and not reachable:
        logger.info("reachability: agent_id=%s reachable=False", agent_id)
    _verdicts[agent_id] = Verdict(reachable=reachable, at=_now())
    while len(_verdicts) > MAX_TRACKED:
        _verdicts.popitem(last=False)


def is_failing(agent_id: str) -> bool:
    """Whether a fresh failed probe stands against `agent_id`."""
    verdict = _verdicts.get(agent_id)
    return verdict is not None and not verdict.reachable and _now() - verdict.at < FAILURE_FRESH_SECONDS


def has_fresh_verdict(agent_id: str) -> bool:
    """Whether the planner already knows enough not to ask again yet."""
    verdict = _verdicts.get(agent_id)
    if verdict is None:
        return False
    window = SUCCESS_FRESH_SECONDS if verdict.reachable else FAILURE_FRESH_SECONDS
    return _now() - verdict.at < window


async def _probe(agent_id: str) -> None:
    """One readiness-grade probe of `agent_id`'s bound endpoint, recorded. Never raises.

    The probe is `operator_readiness`'s own — the stored URL only, through the
    dispatch path's SSRF guard, bounded, nothing about the URL logged — so the
    planner's verdict and the operator's self-check can never disagree about
    what "reachable" means. Imported here, not at module level, because that
    module records into this one.
    """
    from . import operator_readiness

    try:
        record(agent_id, (await operator_readiness.check_reachable(agent_id)).status)
    except Exception as e:
        logger.warning("reachability: agent_id=%s probe outcome=error error=%s", agent_id, type(e).__name__)


def refresh_stale(agent_ids: Iterable[str]) -> None:
    """Probe, in the background, each agent the planner has no fresh verdict on.

    Called by the planner with the bound agents it could route to, so the
    memory stays warm without anyone running the readiness check: a dead
    endpoint is found within one plan of going stale, and a recovered one is
    re-admitted the same way. Never blocks the plan and never raises; at most
    `MAX_IN_FLIGHT` probes run at once, one per agent (single-flight). Outside
    a running event loop there is nothing to schedule on, so it does nothing.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for agent_id in agent_ids:
        if len(_in_flight) >= MAX_IN_FLIGHT:
            return
        if agent_id in _in_flight or not _ID_SHAPE.fullmatch(agent_id) or has_fresh_verdict(agent_id):
            continue
        task = loop.create_task(_probe(agent_id))
        _in_flight[agent_id] = task
        task.add_done_callback(_forget_flight)


def _forget_flight(task: asyncio.Task[None]) -> None:
    """Release a finished probe's single-flight slot (cancelled ones too)."""
    for agent_id, running in list(_in_flight.items()):
        if running is task:
            del _in_flight[agent_id]


def in_flight() -> int:
    return len(_in_flight)


def tracked() -> int:
    return len(_verdicts)


def reset() -> None:
    """Forget every verdict and in-flight probe. For tests."""
    _verdicts.clear()
    _in_flight.clear()
