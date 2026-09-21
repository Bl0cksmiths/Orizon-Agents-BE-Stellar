# ADR 0008 — Paying an upheld dispute: who may order it, and how the credit is paid exactly once

- **Status:** Accepted (story 4.03 / BLO-31), 2026-09-21
- **Deciders:** Danielle (lead)
- **Extends** ADR 0002, which chose the refund *mechanism* — a settler-funded
  platform credit over the asset SAC — and ADR 0007, which decided the window,
  the proof of payer and what settlement has to remember. Both deliberately
  stopped short of adjudication. This decides **who may order a payout**, **how
  the same dispute is never paid twice**, **what happens when the network does
  not answer**, **how much is paid** and **what bounds it**.

## Context

ADR 0002 proved a credit can be paid: a real one landed on testnet on
2026-09-12. ADR 0007 made a dispute durable, provable and judgeable. Neither
moved a dispute past `open`, and both said so. This story is where a human
verdict turns into a signed transfer, which makes it the first route in this
service that spends the platform's **own** balance, and the first one whose
failure mode is paying a buyer twice.

Three facts about the code this lands on shaped the answers more than the
card's text did.

### 1. The existing API-key posture is permissive on purpose, and the reason does not reach here

`require_api_key` (`app/security.py`) is a no-op while `settings.api_key` is
empty — it returns without checking anything — and the public demo runs that
way on purpose. `Settings._money_capable_config_requires_api_key` is the guard
that stops that being reckless, and before this story it was scoped narrowly:
it refused to boot without `API_KEY` only when `STELLAR_SIGNING_KEY` is set
**on mainnet**, or when production PDAX credentials are present. A testnet
signer keeps the demo open, and that is a considered position rather than an
oversight. (The validator now has a third branch, which is D1's; those two are
the posture D1 diverges from.)

The reason it holds for `/api/stellar/server/charge` is specific, and it is
worth naming precisely because it is about to stop holding. That route calls
`PaymentEscrow.charge`, which can only move USDC the payer has already
authorised on-chain, to the agent owner named in that authorization, up to the
amount the payer signed for. An anonymous caller hitting it does not get to
choose whose money moves, where it goes, or how much of it: the authorization
bounds all three, and the payer created it themselves. The worst an open
`charge` route buys an attacker on testnet is the settling of a workflow that
was already paid for.

None of that is true of a refund. The transfer is `settler → buyer` over the
asset SAC, signed by the platform's own key, and the only thing that says it
should happen is an adjudicator's verdict. There is no prior authorization to
bound the recipient, the amount or the fact of it. Anonymous, the uphold route
is a drain on the settler wallet — on **any** network, because the wallet that
funds testnet credits is the same wallet that funds mainnet ones the moment the
deployment flips.

### 2. A payout has a window in which nobody knows whether it happened

`sc.invoke_with_server_key_async` does not raise on failure. It returns
`{"status": "SUCCESS", ...}`, `{"status": "FAILED", ...}` or
`{"status": "timeout", "hash": ...}`, and the third one means *submitted, may
still land*. So between "we decided to pay" and "we know whether the transfer
settled" there is a real interval, and on its far side there is a state in
which the truth is unknown to this process and knowable only from the chain.

Any idempotency scheme that decides whether to pay by **reading** the dispute's
status is therefore deciding on a value that a concurrent payer may be about to
change, and a retry policy that treats "no answer" as "did not happen" is a
policy for paying twice.

### 3. The per-step number a credit would naively be computed from is an estimate

`_record_settlement` writes `SettlementStep.price_usdc=step.est_price_usdc` —
the *plan's* quoted price for that step. What the buyer actually paid is
`settled_usdc`, which `_settled_usdc` derives through the charge's own helper:
`usdc_to_i128(max(total_usdc, 0.000001)) / STROOPS_PER_USDC`, the run's total
floored to dust and rounded to the ledger's seven decimals. Those two numbers
are computed from different inputs by different code, and ADR 0007 already
recorded that the first can exceed the second. A credit taken from the estimate
is a credit the platform may never have collected.

## Decision

### D1 — Adjudication is an authenticated API route that fails closed

`POST /api/disputes/{dispute_id}/uphold` and `POST /api/disputes/{dispute_id}/reject`
are ordinary API routes, and both sit behind `require_adjudicator`, which
**fails closed**: it refuses when `settings.api_key` is empty, on every
network, rather than waving the caller through the way `require_api_key` does.
It also refuses when `settings.dispute_refunds_enabled` is false, so a
deployment that has not turned the money path on cannot be talked into
adjudicating by a caller who knows a dispute id.

This is a deliberate divergence from the posture in Context 1, and the reason
is the asymmetry described there: `charge` spends an allowance the payer
authorised on-chain and cannot exceed it, while an upheld dispute spends the
platform's own balance on an adjudicator's say-so with nothing on-chain to
bound it. Two routes can both be described as "money-moving" and still deserve
different answers, because what bounds them is different.

**`dispute_refunds_enabled` is what makes the strictness affordable.** The
config validator's refund branch fires only when
`dispute_refunds_enabled and stellar_signing_key and stellar_asset_sac` — that
is, when the process is actually capable of signing a transfer — and it then
refuses to boot without `API_KEY` on every network including testnet. Scoping
it to a flag rather than to the presence of credentials is the whole point. A
money path switched on by the mere existence of a signing key would be switched
on in every test run and on every developer's laptop, and the first deployment
to inherit a signing key from an unrelated feature would quietly acquire an
anonymous payout route that nobody chose. With the flag, the exposure names a
deliberate operator decision, and the boot refusal arrives at the moment that
decision is made rather than months later.

The switch defaults **off**, which means the shipped configuration cannot pay
anything at all. That is the correct default for a route whose failure mode is
an emptied wallet, and it costs an operator one environment variable.

**The boot validator is not a sufficient backstop on its own**, which is why
the check is made again per request at the door of the routes. The validator
fires only when `dispute_refunds_enabled` is set *together with* a signing key
and an asset SAC — the configuration that can actually sign. A deployment that
turns the switch on before wiring the signer boots happily with `API_KEY`
empty, and would then serve these routes to anyone. So `require_adjudicator`
answers the three cases in the order a caller meets them: switch off is 503
`dispute_refunds_disabled`, switch on with no key is 503
`adjudication_not_configured` and is logged at ERROR because it is a live
refund switch with no credential behind it, and a missing or wrong key is 401
`invalid_api_key` — the same code `require_api_key` uses, so a client needs one
mapping rather than two. No refusal distinguishes "no key" from "wrong key",
because an adjudication endpoint that did would be an oracle.

**Why not adjudicate from an ops script only.** It is the obvious way to avoid
inventing an authenticated route: no endpoint, no guard, no new surface, and
the settler key is already needed to sign. It was rejected on three grounds.
A script has no record of *who* ran it — the money path's logging obligation is
dispute id, job id, payer and amount on every failure, and a shell history is
not that. It has no idempotency either, unless it reimplements D2, at which
point it is the same code with a worse entry point. And it cannot be reached by
the console, so every adjudication would require shell access to production,
which is a larger standing privilege than an API key that can be rotated. The
script path is not closed — `scripts/prototype_refund.py` still exists for the
manual testnet transfer ADR 0002's evidence needed — but it is a tool for
producing a transaction, not the adjudication path.

**Why not put the routes behind the existing `require_api_key`.** It is one
import and it reads as consistent with the rest of the service. It is also
exactly wrong here: while `API_KEY` is empty that dependency returns
immediately, so on the shipped demo configuration the uphold route would be
**open**, and the first sign of it would be a drained settler wallet. A guard
whose default posture is "allow" is the right shape for a read-scoped demo and
the wrong shape for a payout. The two guards live side by side, named
differently, because they mean different things, and `require_adjudicator`
refusing while `API_KEY` is empty is the one behaviour that must not be
inherited from the other.

### D2 — Idempotency is a claim taken before anything is signed

`refund_claims` is one row per dispute that is mid-payout:

```sql
CREATE TABLE IF NOT EXISTS refund_claims (
    dispute_id  TEXT PRIMARY KEY,
    claimed_at  DOUBLE PRECISION NOT NULL
);
```

and the primary key is the whole mechanism. `INSERT ... ON CONFLICT
(dispute_id) DO NOTHING` is atomic, so of any number of concurrent claimants
exactly one inserts and the rest come back empty. `DO NOTHING` rather than
`DO UPDATE` keeps the losers on the ordinary empty-result path instead of an
exception class every caller would have to name, and leaves the winner's row
untouched.

**Taking the claim and moving the dispute to `crediting` are one statement**,
not two, and that is the part worth recording. The version that tried two is
worth naming because it is the obvious one: insert the claim, read the status
back, append `crediting`. The gap between the insert and the append is a window
the process can die in — Render spins a free instance down whenever it idles —
and what it leaves behind is a claim row over a dispute still reading `upheld`.
Nothing can pay that buyer afterwards: the claim refuses every later claimant,
and a release refuses because the dispute is not `crediting`. They would be
owed money that no code path could send them. A single statement is its own
transaction, so either both rows are there or neither is, whatever happens to
the process in between.

**Concurrency is settled by the primary key, not by the status the statement
reads.** Both CTEs share one snapshot, taken before either ran, so two
claimants racing each other *both* see `upheld` — a status can rule a claim out
and can never arbitrate between two. The unique index is not snapshot-based:
exactly one insert lands, the loser's `DO NOTHING` returns nothing, and the
outer insert selects through the claim, so the loser writes no event row
either. The status gate is still there, on the claim's own `SELECT`, and its
job is the other one: a dispute that is not `upheld` inserts nothing at all.

So **`None` from `claim_refund` means sign nothing** — already claimed, already
credited, still open, or rejected are all the same instruction to the caller.
The claim outlives the process that took it, which is the property a lock in
memory does not have and the reason it is a table.

**Why not a partial unique index over `dispute_events`.** ADR 0007 already
enforces one dispute per `(job_id_hex, step_index)` with exactly that
technique, so reaching for it again is the natural move, and it is wrong for
this rule. `dispute_events` is append-only; a uniqueness rule scoped to "is
crediting" would forbid the **second** claim after a released failure. A buyer
whose transfer definitively failed has not been paid, and must stay payable.
The duplicate-dispute rule is permanent — a dispute of that step happened, for
ever — while the refund claim is a lease that has to be surrenderable. Those
are different lifetimes, and a constraint that cannot express the second one
should not be used for it.

**Why not a Postgres advisory lock.** It would not work here, for a reason
specific to this store rather than to advisory locks. `PostgresDisputeStore`
issues **one statement per call**, and every CTE inside a statement shares the
snapshot taken *before* the lock could have been acquired — so the lock would
be taken and released around reads that had already decided what they would
see. It would guard nothing while looking exactly like a guard, which is worse
than no lock at all.

**Why not read the status, then write it.** The tempting fix, and not a fix at
READ COMMITTED, which is Postgres's default and asyncpg's: both transactions
take their snapshot before either commits, both read `upheld`, and both pay.
Only SERIALIZABLE or an explicit lock would save it, and both cost every
unrelated write in these tables. The same argument, in the same words, is why
ADR 0007's duplicate rule is an index rather than a check in Python; the
conclusion here is a different constraint for a different lifetime, not a
different principle.

**The claim row is cleaned up by the store, not by callers**, and always inside
the statement that writes the transition. `append_status` drops it in the same
statement that records `credited` or `rejected`; `release_refund_claim` drops
it in the same statement that puts the dispute back to `upheld`. The reason is
the one that made the claim a single statement: the mutex and the status are
the same fact recorded twice, and they must not be able to come apart. Dropping
the mutex first would let another payer claim a dispute still reading
`crediting`, which then refuses the credit a buyer is owed; writing the status
first and dying before the delete leaves the unpayable wedge described above.

The release gates on the **status** rather than on the claim row, and that
makes it a repair rather than a guard. A dispute that somehow reached
`crediting` without a mutex row would be stuck for ever if a release refused to
act without one — and `append_status` is public enough that "somehow" is not
hypothetical. Gating on the status returns such a dispute to `upheld`, where it
can be claimed again, and the delete is simply a no-op.

What is left in `refund_claims` is therefore exactly the set of payouts still
in flight, which is what makes the table a reconciliation queue rather than a
pile of spent locks. `list_refund_claims()` is the read that makes that true
rather than aspirational — oldest claim first, with `claimed_at` as the number
that decides which one needs a human now. D3 forbids retrying an unconfirmed
transfer, so the only way a buyer whose refund hung ever gets paid is a person
finding them, and a lock nobody can list is a buyer nobody can find.

### D3 — A timed-out transfer is never retried automatically

`invoke_with_server_key_async` returns `{"status": "timeout", "hash": ...}`
without raising, and that transaction **may still settle**. The dispute stays
in `crediting`, the claim is **not** released, the in-flight hash is recorded
on the dispute, and the event is logged at ERROR with the dispute id, the job
id, the payer and the amount. The adjudicator is answered 504
`refund_unconfirmed`, which says in as many words that it may still land and
must be reconciled rather than retried. A human reconciles it against the
chain.

`credit_refund` classifies the outcome into exactly three, and the third is
deliberately a catch-all: `SUCCESS` **with a hash**, `FAILED`, and *unknown*.
Unknown is not only the literal timeout — an exception from the submit, a
shutdown cancellation landing between the submit and its confirmation, a
`SUCCESS` with no hash, and any status this code does not recognise all map to
it. Every one of those has the same property, which is that the transaction may
be on the network, and a classifier that guessed optimistically about any of
them would be a classifier that pays twice. The consequence is that a stuck
dispute may carry **no hash at all**, when the call raised before one existed;
the reconciliation procedure has to start from the settler account in that
case, and `docs/disputes.md` says so.

`release_refund_claim` exists and is used, but only where nothing was signed —
a missing settlement, a refusal from `creditable_for` or from the cap, all of
which are raised before the transfer — or where what was signed **definitively
failed**: a `FAILED` result, which is the one answer that says no funds moved.
That case releases the claim, leaves the dispute `upheld` and answers 502
`refund_failed`, so a second uphold can pay the buyer who is still owed. An
unknown outcome is not that, and treating it as that is the single mistake that
pays a buyer twice.

**The ban is enforced, not merely documented.** An uphold aimed at a dispute
already in `crediting` is refused with 409 `refund_in_flight` and logged at
ERROR, rather than being allowed to take a fresh claim — so the operator who
tries the obvious thing is stopped by the code, not only by this ADR.

Said plainly: **this trades paying late for never paying twice.** A buyer whose
credit timed out waits for an operator to look, which may be hours. The
alternative — retry on timeout — pays them promptly in the common case and
pays them twice in the case where the first transfer lands after all, out of
the platform's own wallet, with no way to reverse it, because the asset SAC has
no more of an undo than `PaymentEscrow` does. A late credit is a support
ticket; a double credit is an unrecoverable loss that nobody notices until the
wallet is short. The asymmetry is not close, so the automatic behaviour is the
conservative one and the judgement is left to a person with a block explorer.

The operator's side of this — what to check, and the instruction **not** to
simply retry — is written in `docs/disputes.md` rather than left in this ADR,
because the person who meets it is on call, not reading decision records.

### D4 — The credit is the minimum of three numbers

`refund_svc.creditable_for(settlement, dispute, fraction)` is the only place an
amount is produced, and it produces

```
min(dispute.creditable_usdc, step.price_usdc × fraction, settlement.settled_usdc)
```

over the step the dispute names — the amount frozen on the dispute when it was
opened, the policy share of that step's price under the fraction in force now,
and what the charge actually moved — rounded to the ledger's seven decimals.
**Every clamp that actually bites is logged at WARNING with both numbers**,
because a clamp means two records disagree about money, and taking the smaller
one silently is how an overpayment, or a buyer quietly credited less than they
were promised, becomes invisible.

Each term is there for its own reason.

`dispute.creditable_usdc` is the **promise** the buyer was shown. ADR 0007
froze it at opening precisely so a later change to `DISPUTE_CREDITED_FRACTION`
cannot rewrite what a buyer was told, and paying from the record rather than
recomputing from config is what honours that.

`step.price_usdc × fraction` is the **policy share of that one step, now**.
Keeping a live term alongside the frozen one gives the asymmetry that matters:
*lowering* `DISPUTE_CREDITED_FRACTION` applies to disputes that are already
open, while raising it cannot, because the frozen promise still caps the
result. A knob that can only ever reduce the platform's exposure on work
already done is a safe knob; one that can retroactively increase it is not.

`settlement.settled_usdc` is the term that does the real work, and Context 3 is
why. The per-step figure **originates as an estimate** — `est_price_usdc`, the
plan's quote, which the planner wrote before the step ran and which
`dispute_svc` froze the buyer's figure from at opening — while `settled_usdc`
is derived through the charge's own helper from what was actually submitted,
floored to dust and rounded to seven decimals. The two are computed by
different code from different inputs and are not guaranteed to agree. Refunding
the estimate would mean the platform paying back money it never took, out of
its own wallet.

No single one of the three is safe on its own, so the rule is the minimum of
all three rather than a preference order between them. It costs two
comparisons.

Two refusals fall out of the same function and are raised before anything is
signed: `nothing_to_credit` when the settlement has no such step, when the step
never delivered — it was never part of what the buyer paid for — or when the
bounds compute to zero, and `refund_above_cap` for D5.

### D5 — A dedicated refund cap, separate from the charge cap

`max_refund_usdc` ships at `1.0` and is checked **before anything is signed**;
a refund above it is refused and the refusal is logged with the dispute id, the
job id, the payer and the amount.

It is deliberately **not** `max_charge_usdc`, and the two are not merged even
though both are USDC ceilings on a single on-chain transfer. `max_charge_usdc`
(100) bounds what a **buyer** authorised themselves to spend; `max_refund_usdc`
bounds what the **platform** pays out of its own wallet on an adjudicator's
say-so. Sharing a number between them would be a coincidence dressed up as a
control, and the first time one of them needed tuning the other would move with
it silently.

The default is chosen against what this deployment actually settles: a step
costs hundredths of a USDC, so `1.0` is far above anything legitimate and still
keeps the blast radius small — of a leaked settler key, of a mistaken uphold,
or of an amount that arrived at the transfer wrong. Checking it before signing
rather than after is what makes it a control rather than a report.

## Consequences

**A deployment that wants to pay refunds must set `API_KEY`, and one that sets
`DISPUTE_REFUNDS_ENABLED=true` without it will not start.** The refusal is a
`ValueError` from the settings validator at import time, naming the exposure,
on every network — so it fails the Render deploy loudly rather than serving an
anonymous payout route. This is the first setting in this service that makes
`API_KEY` mandatory on testnet, and it is the one place the demo's open posture
does not reach.

**Adjudication is a standing privilege held by whoever holds `API_KEY`.** The
same key already opens `/api/stellar/server/*` and the secured PDAX routes, so
this does not create a new secret, but it does widen what that one secret
authorises. Rotating it now revokes adjudication too, which is the right
coupling but worth knowing before an incident.

**A dispute can get stuck in `crediting`, and only a human gets it out.** That
is D3 working, not failing. It needs an operator procedure, a place to find the
stuck disputes — `refund_claims` holds exactly them — and the discipline not to
re-run the payout, all of which is in `docs/disputes.md`.

**The platform must hold the asset.** ADR 0007 already said the exposure is
bounded per workflow; this story makes it real. A settler wallet without USDC
turns every upheld dispute into a failed transfer, a released claim and a buyer
who was told their claim stood and got nothing. That failure is loud in the
logs and invisible to the buyer, which is the combination that needs watching.

**The lifecycle gained a status that is not a verdict.** `crediting` is the
claim made durable, not something anybody adjudicates into, and the buyer-facing
documentation has to say so — a buyer who sees it should read "your credit is
being paid", not "your dispute is being reconsidered".

**4.04 inherits an `upheld` dispute it can rate.** The on-chain dispute rating
is still that story's, still written under `refund_svc.dispute_job_id(job_id)`
for ADR 0007 D5's reason, and nothing here writes a rating. A dispute may reach
`credited` before its rating exists; the record carries `refund_tx` and
`rating_tx` separately so neither waits on the other.

Related: ADR 0002 (the credit mechanism and the trust model), ADR 0007 (the
window, the proof of payer and the settlement record), `docs/disputes.md` (the
buyer- and operator-facing version, including the reconciliation procedure),
and the code these decisions live in — `app/config.py` (D1's boot refusal, D5's
cap), `app/security.py` (`require_adjudicator`), `app/routers/disputes.py` (the
two routes), `app/services/dispute_svc.py` (the adjudication order),
`app/services/dispute_store.py` (the claim) and `app/services/refund_svc.py`
(the amount and the three-way outcome).
