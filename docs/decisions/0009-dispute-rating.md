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
`app/services/dispute_rating.py`, it replaces `refund_svc.dispute_job_id(job_id)`,
which this story retires, and `tests/test_dispute_job_id.py` pins it against
golden vectors computed independently of the function.

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
   contributes two to it: the automatic rating that says what the step
   delivered, and the dispute rating that says the buyer's claim against it
   stood.
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
dispute **without** one has paid the buyer and has no rating it can vouch for:
a failed submit, a collision (D4), a submission that timed out without
returning a hash, a deployment not configured to rate, a rating that could not
be formed from the dispute's records, or one that landed while the write
recording it failed — the one case with a rating on-chain and none on record,
which is logged with its hash for exactly that reason. That dispute is not
fully resolved, and it is reported that way everywhere the record is read —
`GET /api/disputes/{id}` already carries `rating_tx`.

**Re-upholding a credited dispute retries the rating only.** In 4.03 an uphold
aimed at a `credited` dispute returned it unchanged and signed nothing, which
is the credit's own retry guarantee. 4.04 keeps the transfer half of that
exactly — the `credited` branch still returns above the claim, so it can never
reach the transfer — and adds the rating to it. An operator whose dispute
shows no `rating_tx` retries by upholding again, and the worst that retry can
do is be told the rating already landed.

### D4 — The replay guard is the idempotency

> **Amended 2026-09-25 (story 4.07 — the Epic 4 hardening pass).** The outcome
> table below describes only what each answer writes to `rating_tx`, which was
> the whole record when this was written. Story 4.06 added a second column to
> all three of the writing rows — `rating_confirmed` — and that flag, not the
> hash, is what the buyer's receipt reads.
>
> It exists because `rating_tx` cannot answer the question a receipt asks. A
> submission that timed out records its in-flight hash exactly as a successful
> one records its landed hash, so a set `rating_tx` alone cannot distinguish
> "the agent was rated" from "a rating is in flight and may never land". Three
> values, and only three:
>
> - **`SUCCESS` → `rating_confirmed` True.** The ledger answered, with a hash.
> - **`TIMEOUT` → `rating_confirmed` False.** The hash is recorded and the
>   outcome is not known; the next uphold settles it.
> - **`REPLAY` against a `rating_tx` already on record → `rating_confirmed`
>   True.** The refusal is the confirmation (that is this section's argument),
>   so it upgrades the flag on a hash already held, writing no new one. This is
>   the one move from False to True, and it is why re-upholding a dispute whose
>   rating timed out is worth doing even when the hash is already there.
>
> `FAILED` and a collision write neither field, as below. `null` is "no rating
> was submitted, or the dispute predates the field" — never "no". Anything
> written by hand during a reconciliation has to set the flag too, or the
> receipt shows the agent's consequence as pending for good;
> `docs/disputes.md`'s operator procedures carry it on every `append_status`
> line for that reason.

The refund path needed a durable mutex — `refund_claims`, taken before anything
is signed (ADR 0008 D2) — because the chain cannot stop a second transfer. The
asset SAC will move the same amount to the same buyer as many times as it is
asked, so paying exactly once had to be enforced here, off-chain, and a
timed-out transfer could never be retried.

**Ratings do not need one.** Once the derived id is unique per dispute (D1),
the ledger's own replay guard refuses a second rating for it, at simulation,
before a transaction exists (Context 3). The chain is the mutex, and it is a
better one than a table: it cannot be released by hand, it cannot drift from
what actually landed, and it holds across every process and every deployment
that signs as the Scorer. So a rating is **always safe to retry**, including
after a timeout — the exact case in which a refund never is. A retry either
lands, because the first attempt never did, or is refused as a replay, because
it did. The card's *"one reputation consequence per dispute"* is enforced by
the contract, not by this service.

That makes `Replay` the one ledger refusal that can mean success, and
`dispute_rating.RatingStatus` keeps it apart from `FAILED` for that reason.
What it means for a given dispute turns on that dispute's own history, which
`submit_dispute_rating` does not hold and does not guess at; `uphold` decides:

| outcome | what the chain said | what `uphold` does |
| --- | --- | --- |
| `SUCCESS` | landed, with a hash | records it as `rating_tx`, invalidates the agent's cached score (D5), traces it on the workflow |
| `TIMEOUT` | submitted, unconfirmed — may still land | records the in-flight hash as `rating_tx` when there is one; the next uphold settles it |
| `FAILED` | refused or failed, nothing written | records nothing; the next uphold retries |
| `REPLAY`, `rating_tx` on record | an earlier attempt of ours landed | keeps the recorded hash, invalidates the cached score |
| `REPLAY`, nothing on record | a rating under this id that this dispute has no record of writing | **a collision** — logged at ERROR, recorded as nothing, never reported as resolved |

**The collision rule is deliberately the loud one.** A `Replay` with no prior
attempt on record could be one of two things. It could be a genuine collision
— a rating under this agent and this derived id that some other write put
there — which is unreachable by chance and therefore means something is wrong
that a person has to look at. Or it could be **our own attempt whose hash we
never recorded**: a submit that timed out without returning a hash, a process
that died between submitting and writing the record, a store that was down at
the moment it was written. From inside the service those two are
indistinguishable. Both are answered as a collision, and the error line tells
the operator which facts decide it.

Failing loud is the right direction for that ambiguity, and the asymmetry is
the same one ADR 0008 D3 turned on. Read an unrecorded attempt of our own as a
collision, and an operator spends a few minutes finding the rating on-chain by
its derived id and recording its hash — the rating was never missing, only its
receipt. Read a real collision as our own attempt, and a dispute is reported as
rated when its rating was never written: the agent keeps a clean record, the
`disputed` counter never moves, and nothing anywhere says so. That is exactly
the silent failure the card's last acceptance criterion forbids — *"rather than
swallowing the failure and reporting the dispute as fully resolved"*. A loud
wrong answer is a support ticket; a quiet wrong answer is a missing fact on a
public ledger that nobody will ever go looking for.

**Why not take a claim, as the refund does.** It would buy nothing the guard
does not already give, and it would bring the refund path's worst failure mode
with it: a lease that outlives the process that took it and has to be
reconciled by hand. A rating has no state in which a retry is dangerous, so it
has no need of a lock that makes retries impossible.

**Why not check the ledger before submitting.** The contract exposes no read
for a single `Rated` key — its views are `rep_state`, `avg_bps`, `rep_bps`,
`dispute_rate_bps` and `payer_weight` — and a check-then-submit would be a
read-then-write in any case, deciding on a state a concurrent submit could
change. Submitting and letting the guard answer is both the only way to ask
and the atomic one.

### D5 — The cached score is invalidated, and a stale read cannot write it back

Every reputation read goes through `app/stellar/cache.py` under one key per
agent, `repstate:{agent_id}`, for `REPUTATION_READ_TTL_SECONDS` (15 s). That is
the only cache in front of reputation — the decompose snapshot, both
`/api/stellar/reputation` routes and the dashboard all read through it — and
without invalidation, a plan decomposed inside the window after a dispute
rating lands would be routed and stamped on the very score the dispute was
meant to change. The card's acceptance criterion is *"the plan should use the
updated score, not a cached pre-dispute value"*.

So when a dispute rating is known to be on-chain — a `SUCCESS`, or a `Replay`
against a `rating_tx` already on record, since a rating that timed out may
have landed since — `uphold` calls `reputation_svc.invalidate_rep(agent_id)`,
which calls `cache.invalidate(key)`. On `SUCCESS` it does so **before**
writing `rating_tx` to the store: the rating is on the ledger whatever happens
to the store next, so the score must be fresh even if that write fails.

**Dropping the entry is the easy half, and on its own it does not work.** The
cache is single-flight: the first miss on a key spawns one read, every
concurrent miss awaits that same read, and the result is written back for a
full TTL when it lands. That leaves two races a plain delete does not close.

1. **A stale in-flight read writes back.** A read that started before the
   rating landed saw the ledger from before it. If it finishes after the
   invalidation, it writes the pre-dispute state straight back into the cache —
   for a full TTL, undoing the invalidation as if it had never happened.
2. **A later caller joins a stale flight.** A caller arriving after the
   invalidation, while that same read is still running, finds a flight
   registered for the key and — single-flight working as designed — joins it,
   and is handed the pre-dispute state.

**The generation guard closes both.** `invalidate(key)` bumps a per-key
generation, and every flight captures the generation current when it was
**registered**. A flight may write its outcome back only while its generation
is still current, checked at the write itself with no `await` between the check
and the store, so on one event loop nothing can slip in between. That closes
the first race. `invalidate` also **detaches** the running flight from the key,
so the next caller finds none and starts a fresh read, which closes the second.
The failure cache is fenced the same way, because a negatively cached error
from before the change says nothing about the state after it.

The stale flight is **not cancelled**. Its own callers asked before the rating
landed and get the answer that was true when they asked; it simply stops being
the cache's answer for anyone after. Cancelling it would turn a read that was
merely early into an error for callers who did nothing wrong.

The generation map is bounded by the flights still running, not by every key
ever invalidated: a key's generation only has to outlive the flights that
captured it, so it is dropped when the last of them lands, and an invalidation
with no flight running records no generation at all.

**Why not simply wait out the TTL.** Fifteen seconds is short, but it is not
what the card promised — *"the next plan"* — and the first race makes it
worse than it looks: a read already in flight when the rating lands writes the
pre-dispute state back with a fresh TTL, so the old score can outlive the
rating by the length of that read plus a full TTL.

**Why not bypass the cache for the disputed agent.** The cache exists because
the dashboard reads the whole registry's reputation on a 15-second poll and
every read is a Soroban simulation. A bypass would have to be remembered per
agent and for how long, which is a second cache with worse semantics.
Invalidating the one key is the change of state the cache already needed a
word for.

## Consequences

**Every disputed step carries two ratings, under two keys, and neither amends
the other.** The settler's automatic rating stays where it was written, under
the job's own id; the dispute adds `dispute_rating.DISPUTE_RATING` under the
derived id, at the same weight (D2). The ledger has no entrypoint that amends
a rating, so this is the only honest reading available: the first rating says
what was delivered, the second says the buyer's claim against it stood.

The score is **10 out of 100**, and its reasons are recorded beside the
constant. It sits below the 20 the settler gives a step that delivered
nothing, because a disputed step was billed and the credit comes out of the
platform's wallet rather than the agent's — the agent keeps what it was paid
for work that failed the buyer, and a failure somebody paid for is worse
evidence than one nobody did. It is not 0, because the verdict is the
platform's alone, with no on-chain arbitration and no appeal, and a unilateral
judgement should not carry the harshest score the scale has. The settler's own
scale runs from that 20 — which a billed step can earn too, when it returned an
empty result or an external reply with nothing checkable in it — through 40–95
for work it can check (base 70, moved by the artifact and the critic's pass,
with a baked kit artifact fixed at 95), so at equal weight the two ratings on a
disputed step average between **15 and 52.5**. Both `count` and `disputed` go
up by one, so `dispute_rate_bps = disputed × 10 000 / count` includes the
dispute rating in its own denominator.

**One case in which the dispute rating is the step's only rating — and it is
not new.** The settler keys every automatic rating on the job's own id, so when
one agent served two steps of a job, only the first step's automatic rating
landed; the second was refused as a replay, which the run's trace already
reports as `… : Replay`. A dispute of that second step is therefore the only
rating that step has on the ledger. D1 does not share the limit — each disputed
step derives its own id — but the settler's keying is outside this story and is
left as it is.

**The derivation is frozen for the life of the ledger.** From the first dispute
rating that lands, `dispute_job_id` cannot change without rating agents twice
for disputes retried across the change. The golden vectors hold it; a new
scheme needs a new tag, a new ADR, and a rule for which disputes it applies to.

**A dispute can be paid and not rated, and the record says so.** `credited`
with `rating_tx` null is a buyer who has been credited and an agent whose
reputation consequence is not known to be on-chain. It is visible on `GET
/api/disputes/{id}`, and most of its causes are fixed by upholding again. The
ones that are not say so in the log line: a deployment not configured to rate
names the setting, a rating that could not be formed says the dispute's
records need a person, a landed rating whose record write failed carries the
hash to record by hand, and a collision has its own procedure in
`docs/disputes.md`. A `rating_tx` that was recorded after a timeout is an
in-flight hash, not proof of landing, until an uphold has been answered
`SUCCESS` or `Replay` for it.

**Every upheld dispute costs the Scorer one more submission**, and every repeat
uphold one more simulation. A repeat that finds the rating already landed is
refused at simulation and costs nothing on-chain. A deployment whose signer is
not the ledger's Scorer pays its credits and rates nothing — every dispute
rating is refused `Unauthorized` and logged naming `set_scorer`, and
`/readiness` reports the same thing as `ratings.writer = not_scorer`.

**The payer's stake grows too.** The rating is submitted on behalf of the
dispute's payer, so the ledger's per-payer weight for that agent
(`payer_weight`) accrues the dispute rating as it does the automatic one. That
is the raw per-payer figure the ledger keeps for off-chain Sybil analysis, and
a buyer's upheld disputes now sit in it beside what they paid for.

**The R12 derivation in ADR 0002, and the helpers ADRs 0002, 0007 and 0008
named, are superseded.** The 4.01 spike's helpers are retired from
`refund_svc` in this story, with their tests: `dispute_job_id(job_id)` by
`dispute_rating.dispute_job_id(job_id, step_index)`, `record_dispute_rating`
by `dispute_rating.submit_dispute_rating(dispute, settlement)`, and
`refund_svc.DISPUTE_RATING` by `dispute_rating.DISPUTE_RATING`, still 10.
Leaving the old ones in place would have left a working function that rates
under the id this ADR retired, one import away from the next caller. The three
ADRs keep their bodies as the record of what was decided at the time, and each
carries a dated amendment wherever it names a retired helper as the thing 4.04
would call.

Related: ADR 0002 (the credit mechanism and R12), ADR 0005 D1 (a failed step
is never billed — the reason every rating is weighted by the quoted price
rather than the settled value), ADR 0007 (the dispute record the rating is
derived from), ADR 0008 (the credit the rating follows), `docs/disputes.md`
(reading the two on-chain artifacts, and the collision procedure),
`docs/reputation.md` (what a dispute rating does to a score), and the code
these decisions live in — `app/services/dispute_rating.py` (D1, D2 and the
outcome classification), `app/services/dispute_svc.py` (`uphold`, D3 and D4),
`app/services/reputation_svc.py` and `app/stellar/cache.py` (D5), and
`tests/test_dispute_job_id.py` (the golden vectors).
