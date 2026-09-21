"""Where a settlement and its disputes live — durably (story 4.02, ADR 0002).

A dispute window is a promise made to a buyer at settlement time: *you may
dispute this work until 14:32 tomorrow*. Everything the promise rests on has to
outlive the process that made it, and nothing in `app/state.py` does. That store
holds 200 tasks, **evicts finished ones first** — which is exactly the set a
buyer disputes — and loses all of it on restart, which Render's free tier does
whenever the service idles. A window measured in hours cannot live there.

Nor can the facts a dispute is judged against be recovered afterwards: the
`job_id` is a local in `_settle_onchain`, the payer is a parameter of `_run`, and
there has never been a per-step charged amount or a settlement timestamp
anywhere. So settlement is recorded here, once, at the moment it happens.

The shape follows `binding_store.py` deliberately, down to the lazy driver
import and the append-only tables: same seam, same failure modes, one pattern to
learn. Timestamps are epoch seconds from our own clock, never the database's, so
no timezone conversion sits between what was promised and what is later read.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

# A dispute's lifecycle. `open` is all story 4.02 ever writes; 4.03 pays the
# credit (`credited`) and 4.04 records the on-chain rating, while an
# adjudication that goes the other way ends at `rejected`.
DisputeStatus = Literal["open", "upheld", "credited", "rejected"]

# Retention for the in-memory fallback ONLY — the store that runs when
# DATABASE_URL is unset (local dev and the hermetic test suite). Postgres keeps
# everything; this cap exists so a long-lived local process cannot grow without
# bound, and it is logged when it bites so nobody mistakes a dropped record for
# a bug in the window arithmetic.
_MAX_IN_MEMORY = 500


@dataclass(frozen=True)
class SettlementStep:
    """One step of a settled workflow, as it was charged.

    `price_usdc` is the step's own price — the number a credit for this step is
    computed from. `delivered` is whether the step actually produced output: a
    step that failed was never part of what the buyer paid for, so it cannot be
    disputed (there is nothing to credit).
    """

    step_index: int
    agent_id: str
    agent_name: str | None
    price_usdc: float
    delivered: bool


@dataclass(frozen=True)
class SettlementRecord:
    """What a paid workflow settled as, and until when it can be disputed.

    Frozen because a record is evidence, not state. `settled_usdc` is the amount
    that actually moved on-chain, not the sum of the plan's estimates — the
    charge floors its total to dust, and a credit computed from an estimate
    could exceed what was ever paid.

    `window_closes_at` is stamped here rather than recomputed on read: the buyer
    was told a closing time, and tuning `DISPUTE_WINDOW_SECONDS` afterwards must
    not move the deadline for work already done.
    """

    task_id: str
    payer: str
    auth_id_hex: str
    job_id_hex: str
    charge_tx: str | None
    proof_tx: str | None
    settled_usdc: float
    steps: tuple[SettlementStep, ...]
    settled_at: float
    window_closes_at: float

    def step(self, step_index: int) -> SettlementStep | None:
        """The settled step at `step_index`, or None if this job has no such step."""
        return next((s for s in self.steps if s.step_index == step_index), None)


@dataclass(frozen=True)
class DisputeRecord:
    """One buyer's dispute of one settled step.

    `charged_usdc` is what that step cost and `creditable_usdc` what an upheld
    dispute would credit back under the policy in force when it was opened —
    both frozen at opening time so a later policy change cannot rewrite what the
    buyer was shown.
    """

    id: str
    job_id_hex: str
    task_id: str
    step_index: int
    agent_id: str
    payer: str
    reason: str
    status: DisputeStatus
    charged_usdc: float
    creditable_usdc: float
    opened_at: float
    resolved_at: float | None = None
    refund_tx: str | None = None
    rating_tx: str | None = None


def new_dispute_id() -> str:
    """A dispute id: unguessable, so `GET /api/disputes/{id}` needs no account."""
    return f"dsp_{secrets.token_hex(8)}"


class DuplicateDisputeError(Exception):
    """This step already has a dispute. Carries it, so the caller can return it.

    One dispute per `(job_id, step)` is a product rule, not a database detail:
    the second attempt is answered with the first dispute unchanged rather than
    an error the buyer cannot act on.
    """

    def __init__(self, existing: DisputeRecord) -> None:
        super().__init__(f"step {existing.step_index} of job {existing.job_id_hex} is already disputed")
        self.existing = existing


class DisputeStore(Protocol):
    """The seam between the dispute rules and wherever the records actually live.

    Every method is awaitable even in the in-memory implementation that needs
    none of it, so moving to Postgres is a configuration change rather than a
    rewrite of every call site.
    """

    async def record_settlement(self, record: SettlementRecord) -> None: ...

    async def get_settlement(self, job_id_hex: str) -> SettlementRecord | None: ...

    async def get_settlement_by_task(self, task_id: str) -> SettlementRecord | None: ...

    async def open_dispute(self, record: DisputeRecord) -> DisputeRecord: ...

    async def get_dispute(self, dispute_id: str) -> DisputeRecord | None: ...

    async def find_dispute(self, job_id_hex: str, step_index: int) -> DisputeRecord | None: ...

    async def list_disputes_for_task(self, task_id: str) -> tuple[DisputeRecord, ...]: ...

    async def append_status(
        self,
        dispute_id: str,
        status: DisputeStatus,
        *,
        refund_tx: str | None = None,
        rating_tx: str | None = None,
        resolved_at: float | None = None,
    ) -> DisputeRecord: ...

    async def close(self) -> None: ...


class InMemoryDisputeStore:
    """The fallback when DATABASE_URL is unset: local dev and the test suite.

    Bounded and insertion-ordered. It is NOT durable, and says so at the one
    moment that matters — when a record it was given is dropped — because a
    dispute that silently evaporates is worse than a feature that was never
    offered.
    """

    def __init__(self) -> None:
        self._settlements: OrderedDict[str, SettlementRecord] = OrderedDict()
        self._disputes: OrderedDict[str, DisputeRecord] = OrderedDict()

    async def record_settlement(self, record: SettlementRecord) -> None:
        self._settlements[record.job_id_hex] = record
        self._settlements.move_to_end(record.job_id_hex)
        while len(self._settlements) > _MAX_IN_MEMORY:
            dropped, _ = self._settlements.popitem(last=False)
            logger.warning(
                "in-memory dispute store full (%d): dropped settlement %s — its window can no longer be honoured;"
                " set DATABASE_URL to persist settlements",
                _MAX_IN_MEMORY,
                dropped,
            )

    async def get_settlement(self, job_id_hex: str) -> SettlementRecord | None:
        return self._settlements.get(job_id_hex)

    async def get_settlement_by_task(self, task_id: str) -> SettlementRecord | None:
        return next(
            (r for r in reversed(self._settlements.values()) if r.task_id == task_id),
            None,
        )

    async def open_dispute(self, record: DisputeRecord) -> DisputeRecord:
        existing = await self.find_dispute(record.job_id_hex, record.step_index)
        if existing is not None:
            raise DuplicateDisputeError(existing)
        self._disputes[record.id] = record
        while len(self._disputes) > _MAX_IN_MEMORY:
            dropped, _ = self._disputes.popitem(last=False)
            logger.warning(
                "in-memory dispute store full (%d): dropped dispute %s — set DATABASE_URL to persist disputes",
                _MAX_IN_MEMORY,
                dropped,
            )
        return record

    async def get_dispute(self, dispute_id: str) -> DisputeRecord | None:
        return self._disputes.get(dispute_id)

    async def find_dispute(self, job_id_hex: str, step_index: int) -> DisputeRecord | None:
        return next(
            (d for d in self._disputes.values() if d.job_id_hex == job_id_hex and d.step_index == step_index),
            None,
        )

    async def list_disputes_for_task(self, task_id: str) -> tuple[DisputeRecord, ...]:
        return tuple(d for d in self._disputes.values() if d.task_id == task_id)

    async def append_status(
        self,
        dispute_id: str,
        status: DisputeStatus,
        *,
        refund_tx: str | None = None,
        rating_tx: str | None = None,
        resolved_at: float | None = None,
    ) -> DisputeRecord:
        current = self._disputes.get(dispute_id)
        if current is None:
            raise KeyError(dispute_id)
        updated = replace(
            current,
            status=status,
            refund_tx=refund_tx if refund_tx is not None else current.refund_tx,
            rating_tx=rating_tx if rating_tx is not None else current.rating_tx,
            resolved_at=resolved_at if resolved_at is not None else (current.resolved_at or time.time()),
        )
        self._disputes[dispute_id] = updated
        return updated

    async def close(self) -> None:
        """Nothing to release — kept so the seam is one shape, not two."""
        return None
