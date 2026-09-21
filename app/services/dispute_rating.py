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
