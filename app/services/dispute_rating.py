"""The on-chain reputation consequence of an upheld dispute (story 4.04).

An upheld dispute costs the buyer nothing — the platform credits them (4.03) —
and without this module it would cost the agent nothing either. Here the
settler writes a low, `dispute`-kind rating to the ReputationLedger, so the
agent's `dispute_rate_bps` rises and its score falls, and non-delivery costs it
future routing rather than one refund.

The obstacle is R12. `ReputationLedger.submit` guards on `Rated(agent_id,
job_id)` and returns `Error::Replay` before it ever reads `kind`, and the
settler has already auto-rated every step of every settled job under that very
pair. So the dispute rating is recorded under a DERIVED job id — distinct from
the job's own key, deterministic, and unique per disputed step.

That same guard then becomes the idempotency. Once the derived id is unique per
dispute, the contract itself refuses a second rating for it, so a retry needs
no mutex of the kind the refund path keeps: a retry whose submit is rejected
with `Replay` is a retry whose earlier attempt already landed.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Literal

from ..stellar import client as sc
from . import reputation_svc
from .dispute_store import DisputeRecord, SettlementRecord

logger = logging.getLogger(__name__)

# Domain separation for the derived id. Versioned, so a future change to the
# derivation can never produce an id that collides with one already written
# under this scheme: the replay guard is permanent, and so is every key it has
# seen.
DISPUTE_ID_TAG = b"orizon-dispute:v1"

# The sealed job id is 16 bytes (`BytesN<16>` on-chain); the step index is packed
# into two, which bounds a plan at 65,536 steps — far past anything a plan has.
JOB_ID_BYTES = 16
_STEP_INDEX_BYTES = 2
# How much of the sealed job id the derived id keeps verbatim. Half: enough that
# a reviewer on Stellar Expert can match the rating to the job by eye, and the
# other half left for the hash that makes each disputed step's id unique.
_LINKED_PREFIX_BYTES = 8


def dispute_job_id(job_id: bytes, step_index: int) -> bytes:
    """The job id a dispute's rating is recorded under (R12, amended by 4.04).

    `job_id[:8] ‖ sha256(job_id ‖ DISPUTE_ID_TAG ‖ step)[:8]`.

    The first half IS the sealed job's, so the rating is linked to the job it
    disputes in plain sight: SOW §6.1 needs a reviewer who opens the rating
    transaction to tie it back to the attested job without reading this code,
    and a fully hashed id would make them compute that link rather than see it.

    The second half carries the step. Story 4.01 derived the id from the job
    alone, which collides whenever one agent serves two steps of a job — and a
    plan may do exactly that — so the second upheld dispute's rating would have
    been rejected as a replay of the first. No dispute rating had ever been
    written under that scheme, so replacing it cost nothing.
    """
    if len(job_id) != JOB_ID_BYTES:
        raise ValueError(f"a sealed job id is {JOB_ID_BYTES} bytes, got {len(job_id)}")
    if not 0 <= step_index < 2 ** (8 * _STEP_INDEX_BYTES):
        raise ValueError(f"step index {step_index} does not fit the derived id")
    digest = hashlib.sha256(job_id + DISPUTE_ID_TAG + step_index.to_bytes(_STEP_INDEX_BYTES, "big")).digest()
    derived = job_id[:_LINKED_PREFIX_BYTES] + digest[:_LINKED_PREFIX_BYTES]
    # Equal only if eight hash bytes happen to reproduce the job id's own tail —
    # one chance in 2**64. Checked anyway, because if it ever happened the
    # dispute rating would land on the key the settler's auto-rating already
    # holds and be refused as a replay of it forever. Refusing to derive is
    # loud; a rating that can never be written is not.
    if derived == job_id:
        raise ValueError(f"the dispute id for job {job_id.hex()} step {step_index} equals the job id itself")
    return derived


# What the chain said about one dispute-rating submit.
#
# REPLAY is kept apart from FAILED because it is the one failure that can mean
# success: the contract refuses a second rating under a derived id it has
# already seen, so a retry answered with REPLAY is a retry whose earlier attempt
# landed. Whether it means THAT, or a genuine collision with somebody else's
# key, is not something this module can tell — it depends on whether the
# dispute has a prior attempt on record, which only the caller holds.
#
# TIMEOUT means submitted and unconfirmed: it may still land. A rating is safe
# to retry after one, unlike a refund, because the replay guard makes a second
# landing impossible.
RatingStatus = Literal["SUCCESS", "FAILED", "TIMEOUT", "REPLAY"]


@dataclass(frozen=True)
class RatingOutcome:
    """The result of one attempt to write a dispute rating.

    `tx_hash` is None whenever nothing was submitted — a REPLAY is refused at
    simulation, before any transaction exists — and is the in-flight hash on a
    TIMEOUT. `job_id_hex` is the DERIVED id the rating was written under, the
    one a reviewer finds on Stellar Expert.
    """

    status: RatingStatus
    tx_hash: str | None
    job_id_hex: str
    rating: int
    weight_stroops: int


# The score an upheld dispute writes, on the 0..100 scale of the settler's own
# `reputation_svc.synthetic_rating`, whose anchors are 20 for a step that
# delivered nothing (a timeout, a raise, an empty reply, or an external reply
# with nothing checkable in it), 95 for a baked kit artifact, and 40 to 95 for
# the work in between — base 70, moved by the artifact and the critic's pass.
#
# Below 20 on purpose. ADR 0005 D3 fixed the settler's scale so that a reply
# which delivers nothing never outscores an honest failure; an upheld dispute
# sits one step beneath both. The step was billed — a refund is only ever paid
# against a delivered step — and the credit comes out of the platform's wallet,
# not the agent's, so the agent keeps what it was paid for work that failed the
# buyer. A failure somebody paid for is worse evidence than one nobody did.
# Not 0: the verdict is the platform's alone, with no on-chain arbitration and
# no appeal, and a unilateral judgement should not carry the harshest score the
# scale has.
#
# It does not replace what the settler wrote for the step, which stays on the
# ledger under the job's own key — nothing on-chain can amend a rating — so the
# two stand side by side at the same weight and average between 15 and 52.5. A
# dispute costs an agent the clean record it had, and `dispute_rate_bps` counts
# it; it does not erase the evidence of what was delivered.
DISPUTE_RATING = 10


async def submit_dispute_rating(dispute: DisputeRecord, settlement: SettlementRecord) -> RatingOutcome:
    """Write `dispute`'s rating to the ReputationLedger once, and classify the answer.

    `DISPUTE_RATING`, kind `dispute`, from the settler to `dispute.agent_id`,
    under the DERIVED id for the disputed step and on behalf of the payer. The
    weight is `reputation_svc.rating_weight_stroops` of the settled step's price
    (D2) — the helper and the quoted price the settler weighted its own rating of
    that step with, so the two carry exactly the same evidence.

    What this decides is what the chain said, never what that means for the
    dispute: whether a REPLAY is a retry that already landed or a collision
    turns on the dispute's own history, which is the caller's. The mapping:

      - `status == "SUCCESS"` with a hash → SUCCESS.
      - `status == "FAILED"` → FAILED: the ledger rejected the transaction after
        simulation passed, so nothing was written.
      - anything else → TIMEOUT, with the in-flight hash when there is one.
        `"timeout"` is the client's word for submitted-then-lost-track, and an
        unrecognised status or a SUCCESS with no hash is the same unknown. Unlike
        a refund's, this unknown is harmless to retry: if the first submit
        landed, the replay guard refuses the second.

    Raises instead of returning when the rating cannot even be formed: a
    settlement with no such step (`LookupError`) or a job id that will not
    derive (`ValueError`). The refund this rating follows was computed against
    that same step of that same settlement, and `creditable_for` refuses a step
    the settlement lacks — so either is a record that changed under a paid
    dispute, and an outcome would let the caller file it as a rating to retry.
    """
    step = settlement.step(dispute.step_index)
    if step is None:
        logger.error(
            "dispute %s: cannot rate — settlement %s has no step %d, yet a refund was paid against it "
            "(agent %s, payer %s)",
            dispute.id,
            settlement.job_id_hex,
            dispute.step_index,
            dispute.agent_id,
            dispute.payer,
        )
        raise LookupError(f"settlement {settlement.job_id_hex} has no step {dispute.step_index} to rate")

    weight = reputation_svc.rating_weight_stroops(step.price_usdc)
    try:
        derived = dispute_job_id(bytes.fromhex(dispute.job_id_hex), dispute.step_index)
    except ValueError:
        logger.error(
            "dispute %s: cannot rate — job %s step %d yields no derived id (agent %s, payer %s)",
            dispute.id,
            dispute.job_id_hex,
            dispute.step_index,
            dispute.agent_id,
            dispute.payer,
            exc_info=True,
        )
        raise
    derived_hex = derived.hex()
    # Every line below carries the same facts in the same order: the operator
    # reconciling a dispute rating starts from whichever of them they hold, and
    # the derived id is the one Stellar Expert shows. Identifiers only — no key.
    facts = (
        f"agent {dispute.agent_id}, job {dispute.job_id_hex}, derived {derived_hex}, "
        f"rating {DISPUTE_RATING}, weight {weight}, payer {dispute.payer}"
    )

    raw = await sc.submit_rating_async(dispute.agent_id, derived, DISPUTE_RATING, weight, dispute.payer, "dispute")

    raw_hash = raw.get("hash")
    tx_hash = raw_hash if isinstance(raw_hash, str) and raw_hash else None
    status = str(raw.get("status") or "")

    if status == "SUCCESS" and tx_hash:
        logger.info("dispute %s: rating written — tx %s (%s)", dispute.id, tx_hash, facts)
        return RatingOutcome("SUCCESS", tx_hash, derived_hex, DISPUTE_RATING, weight)

    if status == "FAILED":
        logger.error(
            "dispute %s: rating transaction failed on the ledger, nothing was written — hash=%s (%s)",
            dispute.id,
            tx_hash,
            facts,
        )
        return RatingOutcome("FAILED", tx_hash, derived_hex, DISPUTE_RATING, weight)

    logger.error(
        "dispute %s: rating unconfirmed and MAY STILL LAND — a retry cannot double it, the replay guard "
        "refuses a second: status=%s hash=%s (%s)",
        dispute.id,
        status or "missing",
        tx_hash,
        facts,
    )
    return RatingOutcome("TIMEOUT", tx_hash, derived_hex, DISPUTE_RATING, weight)
