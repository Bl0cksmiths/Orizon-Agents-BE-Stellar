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
entrypoint that removes it. It is checked after the caller, the rating range
and the weight, and **before `kind` is read**, and a hit is `Error::Replay` at
simulation, before any transaction exists.

Two consequences follow, and the decisions below lean on both. Every id a
rating has ever been written under is spent **for ever**: there is no undo, no
expiry and no admin override. And a second submit under the same id is refused
by the chain itself. ADR 0008 needed a durable mutex because the asset SAC will
execute a second transfer as readily as the first; the ledger will not execute
a second rating.

## Decision

### D1 — The derived id: the job's own first half, and a hash of the job and the step

```
dispute_job_id(job_id, step) = job_id[:8] ‖ sha256(job_id ‖ "orizon-dispute:v1" ‖ step)[:8]
```

with the step packed as two big-endian bytes. It lives in
`app/services/dispute_rating.py`, it replaces `refund_svc.dispute_job_id(job_id)`
outright, and `tests/test_dispute_job_id.py` pins it against golden vectors
computed independently of the function.

**The second half carries the step**, which is what closes Context 1. Each
disputed step of a job derives its own id, so two upheld disputes against one
agent in one job are two ratings rather than one rating and a refusal. The tag
is domain-separated and versioned, for the reason every other `orizon-*:v1`
string in this service is: a future scheme under another tag derives ids that
have nothing to do with these. Two bytes of step bound a plan at 65,536 steps,
which is far past anything a plan has; a step that does not fit is refused
rather than truncated into a neighbour's id.

**The first half is the sealed job's own bytes**, which is what closes
Context 2. The job id is minted once, in `_settle_onchain`, and appears
verbatim as an argument of the charge, of the attestation seal and of every
automatic rating the settler wrote for that job. A reviewer who opens the
dispute rating on Stellar Expert reads a job id whose **first sixteen hex
characters are that job's**, and makes the link by eye. The rest of the check —
that the second half is the hash of this job and this step — is open to anyone
who wants it, from the formula above, but it is confirmation rather than the
link itself.

Half is the trade between the two jobs the id has. Eight bytes is enough to
match by eye across a handful of transactions and far more than enough to tell
this job from any other (two random 16-byte job ids share a prefix with
probability 2⁻⁶⁴). The other eight are the hash, and they are what separates
one step of the job from the next.

**It is never the job id itself, and that is checked.** The derived id equals
the job id only if eight bytes of SHA-256 happen to reproduce the job id's own
second half — one chance in 2⁶⁴. `dispute_job_id` checks anyway and raises
`ValueError` rather than returning it, because a derived id equal to the job id
lands on the exact key the settler's automatic rating already holds, and would
be refused as a replay of it for ever. A refusal to derive names the job and
the step; a rating that can never be written names nothing. The same function
refuses a job id that is not sixteen bytes, because the ledger's key is.

**The derivation is permanent.** The replay guard remembers every key it has
ever seen (Context 3). Change the formula after a single dispute rating has
landed, and a retry of that dispute derives a **new** id, the guard does not
recognise it, and the agent is rated twice for one dispute — precisely the
outcome the card forbids when it says *"do not work around it by minting fresh
ids per retry"*. So the golden vectors are not an ordinary test: a failure
there means ratings already on the ledger no longer match the code, and the
test file says so. Should a new scheme ever be needed, it ships under a new tag
and applies only to disputes that have never been rated.

**Why not keep 4.01's derivation.** It was already in the code, ADR 0002 chose
it, and ADR 0007 D5 told this story to write under it, so keeping it was the
default. It fails both requirements above. It collides whenever one agent
serves two steps of a job, which would leave the second upheld dispute in such
a job permanently unratable; and it hides the link, so a reviewer could tie a
dispute rating to its job only by reading our code. Replacing it cost nothing
because nothing had been written under it — which is exactly why it had to be
replaced now rather than after the first rating landed.

**Why not a hash-only id, with the formula documented.**
`sha256(job_id ‖ tag ‖ step)[:16]` fixes the collision just as well, and the
formula could be published beside it. But the link is then something a
reviewer has to **compute** — take the job id from the seal, append a tag and
two bytes, hash, truncate, compare — and knowing that recipe is arguably the
insider knowledge the card rules out. A reviewer who has to trust our
documentation to see the link has not verified it. The prefix makes the link
visible and leaves the hash as a check anyone can run.

**Why not carry the job id in a transaction memo.** Keep an opaque id and put
the job id in the memo, where Stellar Expert displays it. The memo would have
to be written by `invoke_with_server_key_async`, which builds and signs every
transaction this backend submits — the charge, the seal, the refund transfer
and every rating. Adding one would change the function the whole money path
shares, for the benefit of one caller, in the week the refund path landed on
it. And how a memo behaves on a Soroban `InvokeHostFunction` transaction had
not been verified live: an evidence mechanism whose evidence value was
unproven, bought with a change to the shared signer. The prefix needs no change
outside `dispute_rating.py`.

### D2 — The weight is the settler's own, over the step's quoted price

The dispute rating is weighted `reputation_svc.rating_weight_stroops(step.price_usdc)`
— the same helper, over the same number, that weighted the settler's automatic
rating of the same step. `SettlementStep.price_usdc` is written from
`step.est_price_usdc` in `_record_settlement`, and `est_price_usdc` is exactly
what `_submit_ratings` hands `synthetic_rating`, so the two ratings a disputed
step ends up carrying weigh the same. The helper's cap and floor come with it:
`REPUTATION_MAX_RATING_WEIGHT_USDC` (100 USDC, the ledger's own `MAX_WEIGHT`)
above, one stroop below.

**The card contradicts itself on this point, and this records which half
won.** Its product rule reads *"Weight by the settled value, consistent with
how every other rating in the system is weighted."* Every other rating in the
system is **not** weighted by the settled value. It is weighted by the step's
*quoted* price, on purpose — `rating_weight_stroops` says so in its docstring
and the README's reputation section says why: a failed step is never billed
(ADR 0005 D1) and is rated all the same, so weighting by settled value would
make every non-delivery rating weightless. Non-delivery settles nothing. The
two halves of the sentence cannot both be honoured, and **consistency won**.

It won for three reasons.

1. **The two ratings on one step are read on one scale.** The ledger's mean is
   `sum_w / weight` over every rating the agent has, and a disputed step
   contributes two to it: the automatic rating that says the step was paid
   for, and the dispute rating that says the buyer's claim against it stood.
   Weigh them by different measures of the same step and the relative force of
   those two facts depends on how far the quote and the charge happened to
   drift — which says nothing about the dispute. Weigh them by the same number
   and an upheld dispute is exactly as heavy as the work it disputes.
2. **There is no per-step settled value to weigh by.** What moved on-chain is
   `settled_usdc`, the *workflow's* total, floored to dust and rounded to seven
   decimals (ADR 0008 Context 3). The only per-step figure anywhere in the
   record is the quote. "The disputed step's settled value" would have to be
   invented, and any invention — a pro-rata share of the total, say — is a
   second weighting rule living beside the first.
3. **The card's underlying intent survives.** The assumption behind the rule is
   that *"a dispute on a large job should move the score more than one on a
   trivial job"*. The quoted price does exactly that; it is the price the buyer
   agreed to for that step.

**Why not weigh it by the amount credited.** That *is* a per-step number that
reflects what actually moved, which makes it the most tempting reading of
"settled value". It would tie the force of a reputational fact to a refund
policy knob: at `DISPUTE_CREDITED_FRACTION = 0.5` an upheld dispute would weigh
half of the work it disputes, and an operator tuning how generous credits are
would silently be tuning how much a dispute hurts. And the credit is
`min(...)` of three numbers (ADR 0008 D4), so its size can change for reasons
that have nothing to do with the agent — a clamp against `settled_usdc`, for
one. Refund policy and reputation policy are separate decisions and keep
separate numbers.

### D3 — The rating follows the credit, and never touches it

The rating is written only once the dispute is `credited`: after the transfer
has landed **and** the store has recorded it with its `refund_tx`. It is the
last thing `uphold` does, and it is weighed against the same settlement the
credit was just bounded by.

It goes last for two reasons. The card puts it there — *"reputation is written
only after the dispute is upheld and the credit has moved"* — and the two
writes have opposite retry properties. A refund must never be retried blind
(ADR 0008 D3); a rating may always be (D4). Putting the write that can always
be retried after the one that cannot means nothing about the rating can block,
delay or undo the payout. It also leaves ADR 0008's order exactly as it was
reviewed: the rating adds nothing before the claim, nothing between the claim
and the transfer, and nothing on any path that ends in `crediting`, so every
argument that ADR makes about paying once still holds word for word.

**A failed rating never reverses the refund.** Whatever the rating does —
fails, times out, is refused, collides, or raises — the dispute stays
`credited`, its `refund_tx` stays, and the buyer keeps the credit. There is no
path from the rating back into the refund code, and `uphold` answers with the
dispute as it stands rather than with an error: by then the money has moved,
and a 5xx would tell the adjudicator the adjudication failed when the buyer has
in fact been paid — inviting exactly the retry ADR 0008 exists to make safe.
Every rating outcome is logged with the dispute id, the sealed job id, the
derived id, the agent id and the payer, which is everything needed to find the
rating on-chain or prove it is absent; the card's *"logged and retried out of
band"*.

**The record says whether the reputation consequence is on-chain.** A
`credited` dispute **with** a `rating_tx` had its rating submitted under the
derived id and landed — or, after a timeout, may still land. A `credited`
dispute **without** one has paid the buyer and has **not** put its rating on
the ledger: a failed submit, a collision (D4), or an attempt that raised. That
dispute is not fully resolved, and it is reported that way everywhere the
record is read — `GET /api/disputes/{id}` already carries `rating_tx`.

**Re-upholding a credited dispute retries the rating only.** In 4.03 an uphold
aimed at a `credited` dispute returned it unchanged and signed nothing, which
is the credit's own retry guarantee. 4.04 keeps the transfer half of that
exactly — the `credited` branch still returns above the claim, so it can never
reach the transfer — and adds the rating to it. An operator whose dispute
shows no `rating_tx` retries by upholding again, and the worst that retry can
do is be told the rating already landed.
