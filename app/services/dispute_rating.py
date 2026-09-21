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
