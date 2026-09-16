"""Resolve a bound external agent into a runnable worker (story 2.01 follow-up).

Story 2.01 makes a binding durable; this module is what makes it *useful*. It
sits between the binding store and the two places the orchestrator asks "can
this agent do work?", which have different needs and therefore different
answers here:

  * `resolve_worker` (async) is the DISPATCH path. It reads the binding store —
    the source of truth — so a rebind takes effect on the next step rather than
    whenever a cache happens to expire.
  * `is_dispatchable` (sync) is the PLANNING path. `orchestrator_svc` filters
    candidate agents inside list comprehensions, so it cannot await. Without a
    synchronous answer a bound agent would be dispatchable and never selected,
    which is a binding that survives a restart and still does nothing.

`is_bound` is a third reader of the same set and not an orchestrator path at
all: the marketplace asks it, per agent, on every list read (story 3.05). It is
the only caller that needs "we have not looked" to stay distinct from "no" —
see `_loaded`.

Both dispatch paths fail the same way a missing local worker already does — the agent is
simply not routable — because nothing here is an authorization decision. The
chain-owner check happened at bind time; by the time a binding is in the store
it has already been proved. A read failure that skipped a step is a degraded
workflow, never an escalation, so unlike `external_binding.resolve_owner` this
module fails OPEN in the repo's usual style.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..agents.registry import get_worker
from ..agents.workers.base import Worker
from ..agents.workers.external_http import ExternalHttpWorker
from ..stellar import cache as rcache
from .binding_store import get_binding_store

logger = logging.getLogger(__name__)

# Short: a rebind should take effect within a step or two, and the store read is
# a single indexed lookup, so there is little to buy by caching it for longer.
BINDING_READ_TTL_SECONDS = 2.0

# Agent ids known to have a binding. Consulted by the synchronous callers — the
# planner's routability filter and the marketplace's `bound` read; the dispatch
# path always reads the store.
#
# Kept correct by a load at startup, an add on every successful bind, and a
# removal on every revocation or eviction. That is sufficient because
# render.yaml pins `--workers 1`, so no other process can write a binding this
# one has not seen. If that ever becomes `--workers N`, this set needs a
# periodic refresh (the registry_sync loop is the pattern) — otherwise a bind
# served by worker A stays unroutable on worker B, and worse, an UNBIND served
# by worker A leaves worker B still dispatching to the revoked host.
_bound_ids: set[str] = set()

# Whether `_bound_ids` has ever been loaded from the store. False means the set
# is empty BECAUSE NOTHING HAS READ IT YET — a process before its lifespan ran,
# or a startup load the store could not answer, which `refresh_bound_ids`
# swallows on purpose so an unreadable store at boot cannot stop the service.
#
# `is_dispatchable` does not care: it fails the same way for "unbound" and for
# "we have not looked", and the failure is a skipped step either way. `is_bound`
# cares completely, because it is read by a buyer: an empty set reported as
# `False` tells them a working production agent is broken, which is our own
# ignorance dressed up as a fact about someone else's service.
_loaded = False

# Delays between retries of the startup load, in seconds — each one is how long
# to wait BEFORE the next attempt, so the whole schedule is ~3 minutes and six
# attempts including the one at boot.
#
# A BOUNDED retry rather than a periodic refresh loop, and the difference is not
# cosmetic. This set is not a mirror of mutable remote state the way
# `state.agents` is: with render.yaml pinning `--workers 1` this process is the
# only writer, so once it has loaded, `note_bound` and `note_unbound` keep it
# correct and a periodic re-read would be a database round trip to learn what we
# already know. The single failure being repaired is "the store could not be
# read at BOOT" — the cold serverless Neon case `PostgresBindingStore`'s
# min_size=0 pool is explicitly built for — and that resolves in seconds or it
# is an outage a human needs to see. A loop that quietly retried forever would
# be the third variant of this bug, not a fix for it.
#
# The shape is back-off, not a fixed interval, for the same reason: the likely
# cause clears almost immediately, so the early attempts are close together; a
# cause that survives two seconds is unlikely to clear in the next two, so the
# later attempts stop hammering a database that is already struggling.
_REFRESH_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 15.0, 45.0, 120.0)

# True while consecutive load attempts are failing — flips the failure log from
# a WARNING with a traceback (first) to DEBUG (consecutive), so one unreachable
# database cannot produce one traceback per retry.
_load_failing = False

# The bounded retry, while it is outstanding. None on the healthy path, which is
# every boot whose first load succeeded.
_retry_task: asyncio.Task[None] | None = None


def is_dispatchable(agent_id: str) -> bool:
    """Whether a plan step naming `agent_id` could actually be executed.

    True for a local worker, and for an external agent with a binding.
    """
    return get_worker(agent_id) is not None or agent_id in _bound_ids


def is_bound(agent_id: str) -> bool | None:
    """Whether `agent_id` has an operator endpoint bound — None when unknown.

    The marketplace's read of the set the planner already uses, and a different
    question from `is_dispatchable`. That one answers "could a plan step run
    here?" and is True for every seeded agent, because a local worker executes
    it — nothing about a local worker is an endpoint, so answering this through
    it would report the whole seeded catalog as bound.

    None means we do not know, and is reserved for the state above: the set has
    never been loaded, so its emptiness says nothing about any agent. A
    membership hit is still True whatever `_loaded` says — `note_bound` records
    a bind this process itself served, which is first-hand knowledge that does
    not depend on a startup read having worked.
    """
    if agent_id in _bound_ids:
        return True
    return False if _loaded else None


def note_bound(agent_id: str) -> None:
    """Record a just-completed bind so the agent is routable immediately,
    without waiting for a refresh."""
    _bound_ids.add(agent_id)


def note_unbound(agent_id: str) -> None:
    """Forget a binding that has just gone away, so the agent stops being
    offered to the planner IMMEDIATELY.

    The mirror of `note_bound`, and the more urgent half of the pair. A bind
    that takes a moment to become visible costs an operator one idle step; a
    revocation that takes a moment to become invisible keeps handing signed
    dispatch envelopes — buyer intent, rationale and accumulated context — to a
    host the owner has just declared compromised. There is no refresh on the
    dispatch path to fall back on, so this set going stale in the unsafe
    direction is not eventually-consistent, it is indefinite.

    `discard`, not `remove`: every caller is describing an end state ("this
    agent has no binding"), never asserting a prior one. An unbind of an agent
    that was never in the set is exactly as successful as one that was.
    """
    _bound_ids.discard(agent_id)


async def refresh_bound_ids() -> bool:
    """Load the set of bound agent ids from the store; True if it worked.

    A failure is logged and swallowed: an unreadable store at boot must not stop
    the service — every local agent still routes, and external agents simply
    stay unroutable until this succeeds. `_loaded` stays False when it does not,
    which is how `is_bound` tells an empty set apart from an unread one, and the
    returned bool is what lets `_refresh_with_retry` stop as soon as one attempt
    lands.

    Consecutive failures coalesce — the full traceback once, then DEBUG — on
    registry_sync's `_failing` discipline. A retry schedule that logged a
    traceback per attempt would turn one unreachable database into five.

    The read is a SNAPSHOT of the store, so a bind or unbind this process served
    while it was in flight may have missed it. Those are first-hand knowledge of
    a write that already reached the store, so they are re-applied over the
    result rather than being overwritten by a slightly older truth — the
    dangerous direction being a revoked agent reappearing in the routable set.
    With nothing concurrent, which is every call at startup and every call in
    the test suite, both deltas are empty and this is a plain replace.
    """
    global _loaded, _load_failing
    before = set(_bound_ids)
    try:
        ids = await get_binding_store().list_agent_ids()
    except Exception as e:
        if _load_failing:
            logger.debug("binding registry: bound agent id load still failing: %s", e)
        else:
            _load_failing = True
            logger.exception("binding registry: could not load bound agent ids")
        return False
    bound_meanwhile = _bound_ids - before
    revoked_meanwhile = before - _bound_ids
    _bound_ids.clear()
    _bound_ids.update(ids)
    _bound_ids.update(bound_meanwhile)
    _bound_ids.difference_update(revoked_meanwhile)
    _load_failing = False
    _loaded = True
    logger.info("binding registry: %d bound agent(s) loaded", len(_bound_ids))
    return True


async def _refresh_with_retry() -> None:
    """Keep trying the startup load, on the schedule above, until one lands.

    Runs only when the load at boot failed, and returns the moment one succeeds.
    Gives up after the last delay with a single WARNING that names the
    consequence, because by then the problem is not a cold database and a human
    has to look at it.
    """
    for delay in _REFRESH_RETRY_DELAYS:
        await asyncio.sleep(delay)
        if await refresh_bound_ids():
            logger.info("binding registry: bound agent ids recovered after a failed startup load")
            return
    logger.warning(
        "binding registry: gave up loading bound agent ids after %d attempts over %.0fs. Every "
        "externally operated agent stays unroutable until this process is restarted — the planner "
        "filters on a set that is still empty. Check DATABASE_URL and the database's availability.",
        len(_REFRESH_RETRY_DELAYS) + 1,
        sum(_REFRESH_RETRY_DELAYS),
    )


def start_refresh_retry() -> None:
    """Schedule `_refresh_with_retry` unless the startup load already worked.

    Called from lifespan straight after that load. Idempotent, and a no-op on
    the healthy path — the overwhelming majority of boots create no task at all.
    """
    global _retry_task
    if _loaded:
        return
    if _retry_task is not None and not _retry_task.done():
        return
    _retry_task = asyncio.create_task(_refresh_with_retry())
    _retry_task.add_done_callback(_on_retry_done)


def _on_retry_done(task: asyncio.Task[None]) -> None:
    # registry_sync._on_task_done's reason: nothing awaits this task, so an
    # exception inside it would vanish without a line anywhere.
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("binding registry: bound id retry died: %s", exc, exc_info=exc)


async def stop_refresh_retry() -> None:
    """Cancel the retry and wait for it to unwind (lifespan shutdown).

    Without this a process that shuts down inside a retry delay leaves a pending
    task for the loop to destroy, which is the "task was destroyed but it is
    pending" noise lifespan already goes out of its way to avoid.
    """
    global _retry_task
    task, _retry_task = _retry_task, None
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def resolve_worker(agent_id: str) -> Worker | None:
    """The worker for `agent_id`: a local one, else a bound operator endpoint.

    Local workers win. They are the seeded catalog and cannot collide with an
    on-chain id in practice, but stating the precedence means a binding can
    never shadow a first-party agent even if one somehow shared an id.
    """
    local = get_worker(agent_id)
    if local is not None:
        return local

    async def _fetch() -> str | None:
        record = await get_binding_store().get(agent_id)
        return None if record is None else record.endpoint_url

    try:
        endpoint_url = await rcache.get_or_set(f"binding:{agent_id}", BINDING_READ_TTL_SECONDS, _fetch)
    except Exception:
        logger.exception("binding registry: binding lookup failed for %s — step will be skipped", agent_id)
        return None

    if not endpoint_url:
        return None

    # Constructed per resolution rather than cached: ExternalHttpWorker holds
    # endpoint_url as a plain attribute and re-validates it on every dispatch,
    # so a fresh instance is both cheap and the safe default.
    return ExternalHttpWorker(agent_id, f"external.{agent_id}", endpoint_url)
