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

from dataclasses import dataclass
from typing import Protocol


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
