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

Both fail the same way a missing local worker already does — the agent is
simply not routable — because nothing here is an authorization decision. The
chain-owner check happened at bind time; by the time a binding is in the store
it has already been proved. A read failure that skipped a step is a degraded
workflow, never an escalation, so unlike `external_binding.resolve_owner` this
module fails OPEN in the repo's usual style.
"""

from __future__ import annotations

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

# Agent ids known to have a binding. Consulted only by the synchronous planning
# path; the dispatch path always reads the store.
#
# Kept correct by a load at startup plus an add on every successful bind. That
# is sufficient because render.yaml pins `--workers 1`, so no other process can
# write a binding this one has not seen. If that ever becomes `--workers N`,
# this set needs a periodic refresh (the registry_sync loop is the pattern) —
# otherwise a bind served by worker A stays unroutable on worker B.
_bound_ids: set[str] = set()


def is_dispatchable(agent_id: str) -> bool:
    """Whether a plan step naming `agent_id` could actually be executed.

    True for a local worker, and for an external agent with a binding.
    """
    return get_worker(agent_id) is not None or agent_id in _bound_ids


def note_bound(agent_id: str) -> None:
    """Record a just-completed bind so the agent is routable immediately,
    without waiting for a refresh."""
    _bound_ids.add(agent_id)


async def refresh_bound_ids() -> None:
    """Load the set of bound agent ids from the store.

    Called once at startup. A failure is logged and swallowed: an unreadable
    store at boot must not stop the service — every local agent still routes,
    and external agents simply stay unroutable until this succeeds.
    """
    try:
        ids = await get_binding_store().list_agent_ids()
    except Exception:
        logger.exception("binding registry: could not load bound agent ids at startup")
        return
    _bound_ids.clear()
    _bound_ids.update(ids)
    logger.info("binding registry: %d bound agent(s) loaded", len(_bound_ids))


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
