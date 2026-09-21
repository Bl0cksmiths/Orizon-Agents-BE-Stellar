"""When a buyer may dispute a settled step, and what that dispute records.

Story 4.02, ADR 0002. This module is the gate in front of the money: everything
story 4.03 pays out starts as a `DisputeRecord` written here, so every rule that
decides whether one may exist lives in this one file, stated once, in an order
whose reasoning is written down beside it (see `open_dispute`).

Three modules, three jobs, and keeping them apart is what makes each reviewable:

  - `external_binding` holds the PROOF — the challenge, the nonce lifecycle and
    the signature check, shared with bind and unbind;
  - `dispute_store` holds the RECORDS — the settlement a dispute is judged
    against and the dispute itself, durably, because a window measured in hours
    outlives the process that promised it;
  - this module holds the RULES, and owns the vocabulary the API answers with.

Nothing here touches the chain, and that is a deliberate boundary rather than an
accident of scope: a dispute is a CLAIM. Story 4.03 pays the settler-funded
credit if it is upheld and 4.04 writes the rating, both under an adjudication
this sprint performs off-chain (ADR 0002's disclosed trust model). Opening one
must therefore cost no RPC, submit no transaction and touch no reputation —
`tests/test_dispute_svc.py` asserts that rather than leaving it as a claim.

The authority model in one line: THE PAYER PROVES THEMSELVES WITH A WALLET
SIGNATURE, checked against the payer recorded on the settlement at the moment it
settled. Not the in-memory task token — that dies with the process, and a
24 h window that a restart voids is not a window. The signature mirrors endpoint
binding, so a buyer who has already connected a wallet to pay has nothing new to
learn, and the service holds no credential it could lose.
"""

from __future__ import annotations

import logging

from . import external_binding as eb
from .dispute_store import (
    DisputeRecord,
    SettlementRecord,
    get_dispute_store,
)

logger = logging.getLogger(__name__)

# Re-exported so the router depends on ONE module for the whole dispute flow and
# the message the frontend shows is the message the verifier checks. Aliases
# rather than wrappers: a second copy of the format is a second thing to keep in
# step, and the format only stays trustworthy while there is exactly one.
DISPUTE_MESSAGE_PREFIX = eb.DISPUTE_MESSAGE_PREFIX
dispute_message = eb.dispute_message


class DisputeError(Exception):
    """A dispute was refused, and why — in the vocabulary the API answers with.

    `code` is a stable snake token the frontend maps to a message and the
    envelope in `app/main.py` reproduces verbatim; `status_code` is the HTTP
    status that code always carries, kept here rather than in the router so the
    two cannot drift and so every caller of this service agrees on what a given
    refusal means. `message` is for a human and may name specifics (the time a
    window closed); it is never the place to put anything the caller has not
    already proved they may know.

    `existing` carries the ORIGINAL dispute on a duplicate. That is not an error
    the buyer can act on — they already disputed this step — so the router
    answers it with the dispute they already have, unchanged.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        status_code: int = 400,
        existing: DisputeRecord | None = None,
    ) -> None:
        self.code = code
        # The envelope's own fallback (`main.http_exception_handler`) turns a
        # snake token into a human string exactly this way, so a refusal raised
        # without a message still reads as one rather than as an empty body.
        self.message = message or code.replace("_", " ")
        self.status_code = status_code
        self.existing = existing
        super().__init__(self.message)


def _refuse(code: str, status_code: int, message: str, job_id_hex: str, step_index: int) -> DisputeError:
    """Build a refusal and log it once, server-side, with the job it concerns.

    Returned rather than raised so the call site still reads `raise` — the
    refusals are the substance of this module and hiding them inside a helper
    would make them easy to miss. The reason token is logged but never the
    nonce or the signature: both are live credentials for their window.
    """
    logger.warning("dispute refused: job=%s step=%s reason=%s", job_id_hex, step_index, code)
    return DisputeError(code, message, status_code)


async def issue_dispute_challenge(job_id_hex: str, step_index: int) -> tuple[str, float]:
    """Mint the challenge a buyer signs to dispute `step_index` of `job_id_hex`.

    Returns (nonce, expires_at). Async because it reads the settlement store
    first, and it reads the settlement store for `bind_challenge`'s reason: the
    challenge table is bounded and its key is caller-supplied, so it may only
    ever hold pairs that could really be disputed. Without that, one public
    route would let anyone invent job ids and steps until the table evicts the
    challenges honest buyers and operators are mid-way through signing.

    It refuses an unknown job (`unknown_job`) and a step the settlement does not
    have (`step_not_settled`) — and NOTHING ELSE. Whether that step was
    delivered, whether the window is still open and whether a dispute already
    exists are facts about the workflow's private state, and they are answered
    in `open_dispute`, behind the signature. Minting a challenge for a dispute
    that will be refused costs the caller a round trip and tells them nothing.
    """
    settlement = await get_dispute_store().get_settlement(job_id_hex)
    if settlement is None:
        raise _refuse("unknown_job", 404, "no settled workflow with that job id", job_id_hex, step_index)
    if settlement.step(step_index) is None:
        raise _refuse("step_not_settled", 409, f"that workflow has no step {step_index}", job_id_hex, step_index)
    return eb.issue_dispute_challenge(job_id_hex, step_index)


async def get_dispute(dispute_id: str) -> DisputeRecord | None:
    """One dispute by id, or None. The id is unguessable (`new_dispute_id`), so
    holding one is the authorization to read it — the same trade the receipt
    routes make, and the reason this needs no account."""
    return await get_dispute_store().get_dispute(dispute_id)


async def list_for_task(task_id: str) -> tuple[DisputeRecord, ...]:
    """Every dispute opened against a task, in the order they were opened."""
    return await get_dispute_store().list_disputes_for_task(task_id)


async def settlement_for_task(task_id: str) -> SettlementRecord | None:
    """The settlement a task produced, or None if it never settled.

    What the console needs to show a dispute button at all: the job id to
    challenge against, the steps that were charged, and the stamped
    `window_closes_at` that says whether there is still time.
    """
    return await get_dispute_store().get_settlement_by_task(task_id)
