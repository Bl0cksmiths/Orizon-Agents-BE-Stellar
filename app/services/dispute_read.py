"""The payer proving who they are in order to READ their own disputes (D-067).

A dispute's two free-text fields — the buyer's `reason` and, on a rejection,
the platform's `rejection_reason` — are withheld from anyone who has not proved
they may read the task (`routers/disputes.DisputeResponse.of`). That gate is
right and stays. What was wrong was the only proofs it took: the operator key,
which a payer never has, and the in-memory task token, which dies with the
process, the tab and the 200-task ring. Adjudication can take a day, so by the
time there is a rejection to read, the token is almost always gone.

The payer can always prove themselves another way, the way they proved
themselves to open the dispute: a wallet signature, checked against the payer
the SETTLEMENT RECORD names. Two steps:

  1. `issue_read_challenge(task_id)` mints a nonce under the `dispute_read`
     purpose — its own budget in the shared challenge table, so no amount of
     reading can refuse or spend a DISPUTE challenge.
  2. `grant_read(task_id, nonce, signature_b64)` checks the signature over
     `dispute_read_message(task_id, nonce)` against `settlement.payer`,
     consumes the nonce, and mints a stateless grant
     (`task_auth.mint_read_grant`) the reader sends back as
     `X-Dispute-Read-Grant`.

NONE of the opening rules apply, on purpose: not the window, not the step,
not whether anything was charged, not whether a dispute exists. Those decide
whether a new claim may be made; this decides only whether the party whose
money moved may read what was already said about it — which is as true a week
after the window closed as it was inside it.

Refusals are `dispute_svc.DisputeError`s, so the router maps them exactly as
it maps every other buyer refusal. Every message here states a rule or echoes
nothing: the one fact these routes disclose — whether a task has a settlement
— is already served to anyone by `GET /api/tasks/{task_id}/disputes`.
"""

from __future__ import annotations

import logging

from ..config import settings
from ..state import state
from ..task_auth import mint_read_grant
from . import dispute_svc
from . import external_binding as eb
from .dispute_store import SettlementRecord

logger = logging.getLogger(__name__)


async def _settlement(task_id: str) -> SettlementRecord:
    """The task's settlement, or the refusal that says why there is no payer to prove.

    `no_settlement` when the task is one this process knows but it never
    settled, `unknown_task` otherwise. A task evicted from memory whose
    settlement survived is found by its settlement, which is the point: the
    settlement store is what outlives the ring, and it is the record the payer
    is proved against.
    """
    settlement = await dispute_svc.settlement_for_task(task_id)
    if settlement is not None:
        return settlement
    if task_id in state.tasks:
        raise dispute_svc.DisputeError("no_settlement", "that task never settled, so there is no payer", 404)
    raise dispute_svc.DisputeError("unknown_task", "no task with that id", 404)


async def issue_read_challenge(task_id: str) -> tuple[str, float]:
    """Mint the challenge the payer signs to read `task_id`'s disputes: (nonce, expires_at).

    Refuses BEFORE touching the table — the table is bounded and public, so it
    may only hold keys that could really be proved against, and are worth
    proving (`dispute_svc.issue_dispute_challenge`'s rule):

      * a task with no settlement has no payer to prove (`unknown_task` /
        `no_settlement`, 404);
      * a settled task with NO DISPUTE on it has nothing to read
        (`no_disputes`, 404). Settled alone used to be enough, so any hundred
        settled tasks — whose ids `GET /api/tasks` hands out — held the whole
        `dispute_read` budget and refused every payer who had something to
        read. A dispute takes the payer's own signature to open, so a stranger
        cannot conjure the tasks this now mints for.

    ONE exception to the second rule, and only while TASK_AUTH_REQUIRED is on
    (`_first_dispute_needs_a_grant`): a task with no dispute YET that could
    still be disputed. Under enforcement the listing is where a payer finds
    the job id to raise a FIRST dispute, and it admits them on a task token or
    a grant. The token lives in process memory and dies with a restart, so
    without this a payer inside their window, after a restart, could reach
    neither the listing nor a grant for it — the one hole a restart made in
    5.01 AC4. It does not reopen the budget: under enforcement `GET /api/tasks`
    lists nothing, so the task ids a stranger would spend the budget with are
    not handed out, and the set is bounded by the window exactly as the
    dispute mint's own is (`dispute_svc.could_still_be_disputed`).

    Raises `ChallengeBudgetExhausted("dispute_read")` when that budget is full
    of live challenges, which `main.py` answers as 503
    `challenge_capacity_dispute_read`.
    """
    settlement = await _settlement(task_id)
    if not await dispute_svc.list_for_task(task_id) and not _first_dispute_needs_a_grant(settlement):
        raise dispute_svc.DisputeError("no_disputes", "that task has no disputes to read", 404)
    return eb.issue_dispute_read_challenge(task_id)


def _first_dispute_needs_a_grant(settlement: SettlementRecord) -> bool:
    """Whether the payer needs a grant to START disputing this task (see `issue_read_challenge`).

    Only under enforcement, where the listing is gated; with it off the listing
    is open and a grant for an undisputed task would buy nothing. And only
    while a dispute could still be opened, judged on the stamped settlement
    alone — the record that survives the restart this exists for.
    """
    return settings.task_auth_required and dispute_svc.could_still_be_disputed(settlement)


async def grant_read(task_id: str, nonce: str, signature_b64: str) -> tuple[str, float]:
    """Verify the payer's read signature and mint a grant: (grant, expires_at).

    Cheapest first, each with its own code:

      1. the task has a settlement (`unknown_task` / `no_settlement`, 404);
      2. the nonce is this task's outstanding read challenge — `challenge_unknown`
         (409) when it is not, which is what a REPLAYED nonce is, since a proof
         consumed it; `challenge_expired` (409) when it was and its five minutes
         are up;
      3. the signature is the SETTLEMENT'S payer's over the read message, which
         consumes the nonce (`not_the_payer`, 403). A malformed signature is the
         same answer: there is no request field naming a payer to be told apart
         from, so there is nothing a second code would add but an oracle.

    A failed signature leaves the nonce alone, so a stranger's guess cannot
    cancel the payer's challenge.
    """
    settlement = await _settlement(task_id)
    standing = eb.dispute_read_challenge_state(task_id, nonce)
    if standing == "unknown":
        raise dispute_svc.DisputeError(
            "challenge_unknown", "that is not this task's read challenge, or it was already used", 409
        )
    if standing == "expired":
        raise dispute_svc.DisputeError("challenge_expired", "that read challenge has expired — ask for a new one", 409)
    if not eb.verify_dispute_read_challenge(task_id, settlement.payer, signature_b64):
        raise dispute_svc.DisputeError("not_the_payer", "only the payer of a workflow may read its disputes", 403)
    return mint_read_grant(task_id, settlement.payer)
