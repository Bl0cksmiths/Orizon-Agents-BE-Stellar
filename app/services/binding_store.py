"""
Durable storage for operator endpoint bindings (story 2.01, ADR 0003 D1).

Every other store in this service is process-local and says so —
`AppState` ("contents are lost on restart", app/state.py:14-22) and
`app/pdax/ramp_store.py` both name the database they never got. A binding
cannot join them: AC-5 is literally "the binding survives a backend restart",
and the free Render instance spins down after ~15 minutes idle and comes back
from the image, so a binding in this process (or on this process's filesystem)
is gone before anyone looks at it.

So this module is the first durable write the service makes, and it is shaped
to keep that from costing anything anywhere else:

  - `BindingStore` is a Protocol, and `database_url` picks the implementation
    at runtime. Empty — the default, and what tests and local dev run with —
    selects `InMemoryBindingStore`, so the suite stays hermetic and offline and
    the 82% coverage gate is unaffected. Production sets DATABASE_URL in the
    Render dashboard and gets Postgres with no code change.
  - The dependency is asyncpg and nothing else: no ORM and no migration
    framework, because this is one table. The driver is imported lazily so a
    checkout without it still boots, imports and tests cleanly.
  - The Postgres table is APPEND-ONLY. A rebind inserts a new row rather than
    updating one, so AC-6's timestamp and the audit history come for free and
    `previous_endpoint_url` is derived at read time rather than maintained.

`put` returns the NEW record with `previous_endpoint_url` already populated, so
a caller never has to read-then-write and AC-6 cannot race two rebinds.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# Retention cap for the in-memory store, sized like ramp_store._MAX_RAMPS (500)
# so the process's bounded stores agree on what "bounded" costs.
_MAX_BINDINGS = 500


@dataclass(frozen=True)
class BindingRecord:
    """One (agent_id -> endpoint_url) binding, as proved and stored.

    Frozen because a record is evidence, not state: it is what the store
    accepted at `bound_at`, and the way to change a binding is to make another
    one, not to mutate this.
    """

    agent_id: str
    endpoint_url: str
    # The G-address that proved ownership at bind time — recorded, not
    # re-derived, so the audit trail survives a later ownership change.
    owner: str
    # Epoch seconds, matching the house style for timestamps (app/schemas.py:70)
    # rather than a datetime, so no timezone conversion sits between what was
    # stored and what AC-6 reports.
    bound_at: float
    # The endpoint this binding replaced, or None on a first bind. Derived from
    # the row history, never maintained as its own piece of mutable state.
    previous_endpoint_url: str | None


class BindingStore(Protocol):
    """The seam between the router and wherever bindings actually live.

    Narrow on purpose: three methods, all awaitable even in the in-memory
    implementation that needs none of it, so swapping in Postgres is a
    configuration change rather than a rewrite of every call site.
    """

    async def get(self, agent_id: str) -> BindingRecord | None:
        """The current binding for `agent_id`, or None if it has never bound."""
        ...

    async def put(self, agent_id: str, endpoint_url: str, owner: str) -> BindingRecord:
        """Record a binding and return it, `previous_endpoint_url` populated."""
        ...

    async def close(self) -> None:
        """Release anything held (a connection pool); safe to call twice."""
        ...


class InMemoryBindingStore:
    """Bindings for the lifetime of the process — the DATABASE_URL-less default.

    This is what local dev and the hermetic suite run on, and it does NOT
    satisfy AC-5: a restart loses everything here, which is the whole reason
    `PostgresBindingStore` exists. It is still the right default, because the
    alternative is a suite and a laptop that need a database to boot.

    Retention is bounded the way `app/pdax/ramp_store.py` bounds ramps and
    `app/state.py` bounds tasks: an insertion-ordered map with a hard cap, and
    an EXPLICIT eviction on the insert that would exceed it — never an
    unbounded dict that a long-lived process grows forever. Two deliberate
    differences from ramp_store, both forced by what a binding is:

      - There is no terminal-first tier. A ramp can be `completed`/`failed` and
        therefore cheap to drop; a binding is live until something replaces it,
        so every candidate costs the same and the victim is simply the oldest.
      - A rebind refreshes the entry's position (`move_to_end`), so "oldest"
        means least recently bound rather than first ever seen. Losing the
        binding nobody has touched is the least-bad loss available.

    Eviction is logged at WARNING for ramp_store's reason: the operator's
    endpoint silently disappearing — requiring a re-bind nobody was told about
    — is exactly the failure that must not happen quietly.
    """

    def __init__(self, max_bindings: int = _MAX_BINDINGS) -> None:
        self._max_bindings = max_bindings
        self._bindings: OrderedDict[str, BindingRecord] = OrderedDict()

    async def get(self, agent_id: str) -> BindingRecord | None:
        return self._bindings.get(agent_id)

    async def put(self, agent_id: str, endpoint_url: str, owner: str) -> BindingRecord:
        previous = self._bindings.get(agent_id)
        # Only a NEW agent id can grow the store, so a rebind never evicts.
        if previous is None and len(self._bindings) >= self._max_bindings:
            self._evict_one()
        record = BindingRecord(
            agent_id=agent_id,
            endpoint_url=endpoint_url,
            owner=owner,
            bound_at=time.time(),
            previous_endpoint_url=previous.endpoint_url if previous is not None else None,
        )
        self._bindings[agent_id] = record
        self._bindings.move_to_end(agent_id)
        return record

    def _evict_one(self) -> None:
        """Drop the least recently bound agent so the cap can never be exceeded."""
        victim, evicted = self._bindings.popitem(last=False)
        logger.warning(
            "evicted in-memory binding at cap: agent_id=%s endpoint=%s bound_at=%s cap=%d "
            "(set DATABASE_URL to store bindings durably)",
            victim,
            evicted.endpoint_url,
            evicted.bound_at,
            self._max_bindings,
        )

    async def close(self) -> None:
        """Drop every record.

        There is no pool to release, but close() must mean the same thing for
        both stores — after it, this one holds nothing and is safe to call
        again — rather than quietly being a no-op on one side of the Protocol.
        """
        self._bindings.clear()
