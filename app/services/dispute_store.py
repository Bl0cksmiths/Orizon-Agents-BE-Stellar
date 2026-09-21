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
