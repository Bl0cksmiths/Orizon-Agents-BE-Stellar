# ADR 0009 — The dispute rating: an id a reviewer can read, and a replay guard that is its own idempotency

- **Status:** Accepted (story 4.04 / BLO-32), 2026-09-21
- **Deciders:** Danielle (lead)
- **Supersedes** the R12 derivation ADR 0002 chose — `sha256(job_id ‖ "dispute")[:16]`
  — and **extends** ADR 0007 D5, which put that derivation on the path a
  dispute takes, and ADR 0008, which pays the credit this rating follows. This
  decides **which id an upheld dispute's rating is written under**, **what it
  weighs**, **when it is written relative to the credit**, **how a retry avoids
  rating the agent twice** and **how the next plan comes to see it**.

## Context

ADR 0002 resolved R12 on paper, and ADR 0007 made sure a dispute is born
carrying everything the resolution needs — the job id, the step and the agent —
so the rating pair is computable from the stored record alone. Neither wrote a
rating: `refund_svc.record_dispute_rating` existed, was unit-tested, and had no
caller. This story is the first code that calls `ReputationLedger.submit` with
`kind = "dispute"`, which made it the first to find out whether the derivation
it inherited actually holds. Three facts decided the answers.

### 1. The inherited derivation collides inside one job

`refund_svc.dispute_job_id(job_id)` hashed the job alone. The ledger's replay
guard is keyed on `Rated(agent_id, job_id)`, so that derivation is unique per
*agent and job*, not per *dispute* — and the two are the same thing only while
every agent in a plan serves exactly one step. Nothing makes that so. The
planner is free to hire the same agent for two steps of one plan, nothing
downstream refuses it, and a dispute is raised a step at a time (ADR 0007).

So with two upheld disputes against one agent in one job, both derive the same
id: the first rating lands, and the second is refused with `Error::Replay` — for
a dispute that is real, upheld and paid, whose rating has never been written.
It would have failed on the very path the card's last acceptance criterion
exists to catch, and it would have been this story's own derivation doing it.

It was also the last moment the derivation could be replaced for free. No
dispute rating had ever been written under it, so no key in the ledger depends
on it. That stops being true the moment one lands (D1).

### 2. A rating nobody can tie to its job is not evidence

The card's product rule is specific: the rating must stay *"verifiably linked
to the disputed job on Stellar Expert"*, so that *"a reviewer who opens the
rating transaction can still tie it back to the sealed job and its attestation
without insider knowledge"*. SOW §6.1 lists the dispute rating as one of the
on-chain artifacts it asks for, and an unlinkable one satisfies the code and
fails the evidence requirement.

The ledger does nothing to help. `submit` takes the job id as an opaque
`BytesN<16>` and validates it against nothing — any sixteen bytes pass — so
whatever ties the rating to its job has to be in those bytes. Under 4.01's
derivation it was not: a reviewer on Stellar Expert saw 32 hex characters with
nothing in common with the job id on the charge and the seal, and could make
the connection only by knowing our formula and recomputing it.

### 3. The replay guard is permanent, and it is already what a retry needs

`Rated(agent_id, job_id)` lives in persistent storage — v2 moved it there after
v1's temporary guard expired and reopened the window — and the contract has no
entrypoint that removes it. It is checked after the caller, the rating range and the weight, and
**before `kind` is read**, and a hit is `Error::Replay` at simulation, before
any transaction exists.

Two consequences follow, and the decisions below lean on both. Every id a
rating has ever been written under is spent **for ever**: there is no undo, no
expiry and no admin override. And a second submit under the same id is refused
by the chain itself. ADR 0008 needed a durable mutex because the asset SAC will
execute a second transfer as readily as the first; the ledger will not execute
a second rating.
