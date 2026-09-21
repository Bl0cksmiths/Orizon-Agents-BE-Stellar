# ADR 0007 — The dispute window: its clock, its proof of payer, and what settlement has to remember

- **Status:** Accepted (story 4.02 / BLO-30), 2026-09-21
- **Deciders:** Danielle (lead)
- **Extends** ADR 0002, which chose the refund *mechanism* — a settler-funded
  platform credit — and deliberately left the window itself open. This decides
  **when** a buyer may dispute, **how they prove they are the buyer**, and
  **what settlement has to write down** for either question to be answerable an
  hour later.

## Context

ADR 0002 answered "where does the money for a credit come from". It did not
answer the three questions a buyer actually meets: until when may I dispute,
how do I show this workflow was mine, and against what record is the claim
judged. Story 4.02 is where those are decided, and three facts shaped the
answers more than the card's own text did.

### 1. Nothing about a settled workflow survives the process that settled it

`_settle_onchain` mints the job id itself — `job_id = secrets.token_bytes(16)`
— and hands it back to a local in `_run`. `payer` is likewise a parameter of
`_run`. `_finalize_task` persists `status`, `spent`, the last artifact and the
two transaction hashes; it persists **no payer, no job id, no per-step amount
and no settlement timestamp**. What it does persist lives in `state.tasks`,
which `app/state.py` caps at 200 (`task_order = deque(maxlen=200)`), **evicts
`complete`/`failed` tasks first** — precisely the set a buyer disputes —
and loses entirely on restart, which a free-tier Render instance does whenever
it idles.

So every input a dispute needs is either a local variable that has already gone
out of scope, a number that was never computed per step, or a row that the
store is actively trying to drop. None of it is recoverable afterwards. It has
to be written at the moment it is true, or not at all.

### 2. The capability token cannot carry an authorization that moves money

The card proposes proving the payer "by the existing capability-token pattern
— not by a new account system", and the instinct is right: this marketplace
has no accounts and should not grow one for a dispute. But the token that
exists cannot bear this particular weight. `state.task_tokens` is a plain dict
in the same process-local state as the tasks, evicted in lockstep with them,
and `require_task_read` is a no-op unless `TASK_AUTH_REQUIRED` is on — which it
is not by default, because the public demo stays open.

Read that against the card's own acceptance criterion, *"the dispute survives a
restart"*. A durable dispute record whose only credential died with the process
is not a surviving dispute; it is a record nobody can ever act on. And in the
shipped configuration the guard is off, so the token proves nothing at all —
the same endpoint would accept a dispute from anyone who knows a task id.

### 3. R12 has a resolution, and this is the story that has to put it in force

ADR 0002 chose it: the dispute rating is written under
`dispute_job_id(job_id)`, a derived id that clears the ledger's replay guard
while staying linkable to the disputed job. The card is explicit that a window
must not be built first and the collision discovered in 4.04. So the resolution
belongs in this story's code and in this record, named, not left implicit.

## Decision

### D1 — A 24-hour window, opening when the workflow settles, stamped on the record

`DISPUTE_WINDOW_SECONDS` ships at `86_400.0` — 24 hours — measured from the
moment the workflow settles, which is the same moment `_settle_onchain` charges
and seals. The window opens at settlement rather than at task completion
because settlement is the event that creates something to dispute: before the
charge there is no money at stake, and a run that never charged has nothing to
credit back.

24 hours is a product choice with two constraints on it, and it sits between
them rather than being derived from either. Short enough that an operator's
earnings become final within a day and the platform's exposure to a credit is
bounded; long enough that a buyer in any timezone gets a full waking day to
look at the artifact they paid for. A demo-length window — an hour, say —
would make the feature testable and the promise worthless.

**The closing time is stamped on the settlement record, not recomputed from
config on read.** `SettlementRecord.window_closes_at` is written once, at
settlement, from the value of `DISPUTE_WINDOW_SECONDS` in force at that
instant, and every later check compares against that field.

This is the part worth the ADR. The alternative — store `settled_at` and
evaluate `settled_at + settings.dispute_window_seconds` whenever someone asks —
is one field lighter and wrong in a way that only shows up in production. A
buyer was *told* a closing time: it is on their receipt and in the API response
they got when the workflow settled. Retuning the setting afterwards, for any
ordinary reason — shortening it to cut platform exposure, lengthening it for a
support case — would silently move the deadline for work already done, in both
directions. Shortening it closes windows the platform promised were open;
lengthening it reopens windows an operator was told had closed and whose
earnings they believed final. Neither is a deployment anyone would connect to
the config change that caused it.

The stamp makes the tuning knob mean exactly what an operator expects: it
governs workflows that settle *after* the change, and nothing that already
happened. The same reasoning is written at both ends — beside the setting in
`app/config.py` and on the field in `dispute_store.SettlementRecord` — because
it is the kind of "redundant" field a later cleanup deletes.

Timestamps are epoch seconds from our own clock, never the database's, so no
timezone conversion sits between what the buyer was promised and what is later
read back.

### D2 — The payer proves themselves with a wallet signature, not the capability token

Opening a dispute is authorized by a signature from the payer's own wallet over
a domain-separated message:

```
orizon-dispute:v1:{job_id_hex}:{step_index}:{nonce}
```

verified against `SettlementRecord.payer` — the G-address that authorized the
escrow and whose USDC actually moved — exactly as ADR 0003 D3 verifies a
binding against the agent's on-chain owner. The shape is deliberately the same
one: `orizon-dispute:v1` domain-separates it, so an Orizon dispute signature is
never a valid signature for another protocol or for a binding; the version
lets the format move; and the message covers **what is being authorized** —
this job, this step — not merely a nonce. The nonce is minted per attempt,
single-use, and consumed only on a successful verification, so a failed attempt
cannot be used to burn a live challenge. Both signature encodings ADR 0003
accepts are accepted here for the same reason: wallets do not agree on what
"sign this message" means, and Freighter implements SEP-53 rather than signing
raw bytes. The verification goes through the SDK's `verify_message`, not a
second hand-built copy of the framing.

**Why not the capability token the card suggested.** The token is a fine
credential for what it was built for and the wrong one here, for three reasons
that compound:

1. **It does not survive a restart.** `state.task_tokens` is process-local
   memory. The card's own acceptance criterion is that the dispute survives a
   restart; a durable record whose only key died with the process fails it just
   as completely as losing the record would.
2. **It does not survive 200 tasks.** Tokens are evicted in lockstep with their
   tasks, and eviction prefers finished ones — so on a busy day the token is
   gone well before the 24-hour window is, and it is exactly the settled
   workflows whose tokens go first.
3. **Its guard is off by default.** `TASK_AUTH_REQUIRED` ships false so the
   public demo stays open, and `require_task_read` returns immediately when it
   is false. A dispute endpoint that trusted the token would, in the shipped
   configuration, accept a dispute from anyone holding a task id.

The first two are fatal on their own. The third says what the token *is*: a
read credential, whose worst failure is that someone sees a trace they should
not have, and whose loss costs a buyer visibility. A dispute is a write that
ends with USDC leaving the platform wallet and a 10/100 rating on an operator's
permanent on-chain record. Those are not the same authorization, and the fact
that one of them can be switched off for a demo is the clearest evidence that
they should not share a credential.

**The cost, stated.** The buyer signs once more. They have already signed the
payment authorization, so the wallet is one they own and have used on this
workflow — but a signature is still a prompt, a moment of friction, and one
more thing that can be declined. The frontend story (4.05) therefore needs the
wallet **connected** at dispute time, not merely a remembered address; for a
buyer coming back the next day, that means reconnecting the wallet that paid.
This is not a new dependency — the dispute action lives on the receipt view of
a run that wallet paid for — but it is a real one, and it is the reason the
challenge endpoint returns the exact message to sign rather than leaving the
frontend to assemble it.

One consequence follows and is accepted: **a buyer who no longer controls the
wallet that paid cannot dispute.** There is no recovery path, because there is
no account to recover into. That is the same trade permissionless payment
already makes everywhere else in this system.

### D3 — Settlement is recorded durably, once, at the moment it happens

`app/services/dispute_store.py` records a `SettlementRecord` inside the
settlement path, carrying everything a dispute is later judged against:

| field | why it has to be written here |
|---|---|
| `payer` | a parameter of `_run`; nothing persisted it, and it is now the credential D2 verifies against |
| `job_id_hex` | minted inside `_settle_onchain` as a local; it is the dispute key, the attestation key and the rating key |
| `auth_id_hex` | ties the credit back to the escrow authorization the buyer signed |
| `charge_tx`, `proof_tx` | the buyer's evidence that this workflow was paid and sealed |
| `settled_usdc` | the amount that actually moved on-chain |
| `steps` | per-step `price_usdc` and `delivered` — neither existed anywhere before |
| `settled_at`, `window_closes_at` | there was no settlement timestamp at all, and D1 needs both |

Two of those rows carry a decision rather than a fact.

**`settled_usdc` is what moved, not what was planned.** `spent` accumulates
`est_price_usdc` for delivered steps only, and the charge then floors its total
to dust (`max(total_usdc, 0.000001)`). A credit computed from the plan's
estimate could therefore exceed what the buyer ever paid — the platform
refunding money it never took, out of its own wallet, on a workflow where half
the steps never ran.

**`delivered` is recorded per step** because "this step failed and was never
charged" is a fact about settlement that nothing else preserves: the trace says
it, and traces do not survive a restart. Without the flag, the rule that an
undelivered step cannot be disputed would have nothing to evaluate a day later.

The records are frozen dataclasses because a settlement record is **evidence,
not state** — the only thing that ever changes about a dispute is its status,
and that is appended through `append_status` rather than mutated in place. The
step breakdown is one JSON column rather than a child table: it is only ever
read whole, with the settlement it belongs to, so a join and a transaction
would buy nothing.

The store follows `binding_store.py` deliberately — same seam, same lazy driver
import, same append-only shape, same `DATABASE_URL`-or-in-memory selection, and
the same module-level singleton rather than an `@lru_cache`d resolver for the
reason ADR 0003 D1 records. One pattern to learn, one set of failure modes.
The in-memory fallback is bounded at 500 records and **logs a warning naming
the record it dropped**, because a dispute that silently evaporates is worse
than a feature that was never offered — and because the fallback is what local
dev and the hermetic test suite run on, so the suite stays offline and the
coverage gate is unaffected.

A `DisputeRecord` freezes `charged_usdc` and `creditable_usdc` at opening time,
under the policy in force then. `DISPUTE_CREDITED_FRACTION` is a tuning knob
like the window length, and the same rule applies to it: changing it must not
rewrite what a buyer was already shown. Its id is
`dsp_` + `secrets.token_hex(8)` — unguessable, so a buyer can read their own
dispute back without an account, which is the same trade the task read token
makes and the reason `GET /api/disputes/{id}` needs no credential.

### D4 — What 4.02 deliberately does not do

**Opening a dispute writes `open` and nothing else.** No transaction is
submitted, no rating is written, no reputation moves, and nobody adjudicates.
The full status vocabulary is defined now — `open → upheld → credited`, or
`open → rejected` — so the later stories append to a lifecycle rather than
redesign one, but 4.02 is the only story that never leaves `open`. Story 4.03
pays the credit and records `credited` with its refund tx; story 4.04 writes
the on-chain rating and records `upheld` with its rating tx.

That an open dispute costs the agent nothing is a product rule, not an
implementation gap. A dispute anyone can raise, that immediately dents a
competitor's on-chain score, is a free weapon — and because the ledger is
append-only there would be no way to take the dent back when the dispute was
rejected. So the only thing that ever reaches the chain is an **upheld**
dispute, and until then the disputed agent's reputation reads exactly as it did
before. The buyer is told this plainly in `docs/disputes.md` rather than left
to infer it.

The dispute is emitted into the workflow's trace, so it appears in the run's
own record where a buyer is already looking. That is a convenience, not the
record: traces are in-memory and die with the process, which is the whole
reason D3 exists.

Adjudication itself stays out of scope. ADR 0002 already disclosed that the
platform adjudicates and that there is no on-chain arbitration this sprint;
4.02 does not narrow that further by inventing an automatic rule. What it does
is make adjudication *possible* — a mandatory written reason, the settled
amounts, the per-step delivery flags and the timestamps are all captured, so
whoever reviews the dispute is reading evidence rather than reconstructing it.

### D5 — R12 is resolved, and this is where it is named

**R12** (ADR 0002): `ReputationLedger.submit` checks its replay guard on
`Rated(agent_id, job_id)` *before* it reads the `kind` argument, and the settler
has already auto-rated every step of the settled workflow under that job id. A
`"dispute"`-kind rating on the same pair is therefore rejected with
`Error::Replay` — the dispute is unwritable on-chain, however correct the
off-chain record is.

The resolution is in the code, in one function:

```python
# app/services/refund_svc.py
def dispute_job_id(job_id: bytes) -> bytes:
    return hashlib.sha256(job_id + b"dispute").digest()[:16]
```

Derived rather than random, so it is reproducible from the settled job id alone
and the dispute stays linkable to the job it disputes; distinct, so it clears
the guard. `record_dispute_rating` already writes through it, and
`tests/test_refund_svc.py` pins the derivation.

**What 4.02 does about it, concretely.** The window opens at settlement — after
`_submit_ratings` has run — so the collision is real for every dispute this
system will ever see; there is no timing that avoids it, which is exactly why
the card forbade discovering it in 4.04. What 4.02 guarantees instead is that
the dispute is born with everything the derived id needs: `job_id_hex` and the
disputed `step_index` are on the record, and the step's `agent_id` with them, so
the rating pair `(agent_id, dispute_job_id(job_id))` is computable from the
stored dispute without re-reading anything that no longer exists.

**Story 4.04 must write the dispute rating under the derived id**
(`refund_svc.record_dispute_rating`, which calls `dispute_job_id` for it) and
must not call `submit_rating` with the settled job id. A rating written under
the raw job id will be rejected on-chain, and it will be rejected *silently* as
far as the buyer is concerned — `_submit_ratings` treats a failed rating as
best-effort and moves on. That is the failure this ADR exists to prevent, so it
is stated here as a requirement on the next story rather than a note in a
docstring.

### Why not turn `TASK_AUTH_REQUIRED` on and keep the token

It would close the third objection in D2 and neither of the first two. The
token would still be a process-local dict entry, still evicted ahead of live
tasks once 200 have run, and still gone on the next restart — so a buyer with a
perfectly valid 24-hour window would find their credential had expired in
minutes. It would also change the public demo: every task read route would
start demanding a header, for a guard that does not solve the problem it was
turned on for. The setting stays as it is, doing the read-scoping job it was
written for.

### Why not sign the nonce alone

The 1.06 binding prototype did exactly that, and ADR 0003 D3 rejected it for
binding: a signature captured inside its window could be replayed against a
different URL. The same hole is worse here, because a dispute names two things
that both matter. A nonce-only signature could be replayed against **any step
of any settled job** the holder knows the ids of — including a step of a
workflow the signer never paid for. Naming the job and the step inside the
signed message makes the signature authorize precisely one dispute and nothing
else, and costs a string concatenation.

### Why not keep the settlement facts on the `Task` model

It is the obvious place and it fails on all three of the store's properties.
`Task` lives in `state.tasks`, which is capped, evicts finished tasks first and
empties on restart — the record would be gone before the window closed, which
is the failure D3 exists to prevent. It is also a **response shape**: `Task` is
serialized straight back to any caller that can read the task, so putting the
payer and the authorization id on it would publish them, the same leak
`app/state.py` already avoids by keeping the read token off the model. A
settlement record is evidence with a different lifetime and a different
audience from a task, and it belongs in a different store.

## Consequences

**The settlement path now has a durable write on it.** It runs after the charge
and the seal, so it can never be allowed to fail the workflow — the money has
already moved and the attestation is already on-chain. A settlement that cannot
be recorded is a paid workflow with **no dispute window**, and the buyer has no
way to discover that, so it has to be logged with the same weight as a seal
that did not settle: it is the other state that has to be reconstructable from
the logs.

**Production must set `DATABASE_URL`.** Without it the process falls back to the
in-memory store and every promise in this ADR lasts until the next restart —
which a free-tier instance performs whenever it idles. The store logs that
verdict once at startup and again whenever it drops a record, but nothing
refuses to boot: a local run and the hermetic test suite legitimately have no
database, and that is the same position ADR 0003 D1 took for bindings.

**The platform carries a bounded, 24-hour liability.** For a day after each
settlement, every delivered step of that workflow can become a credit paid out
of the settler's own wallet — never a clawback from the agent. The exposure per
workflow is capped by `settled_usdc` and the credited fraction, and the settler
must actually hold the asset, or an upheld dispute cannot be paid.

**The disputed step ends up rated twice.** The settler's automatic rating stays
where it is, and an upheld dispute adds a second, low rating (10/100) under the
derived job id. That is the price of resolving R12 without a contract change,
and it is the honest reading of the ledger: the first rating says the work was
paid for, the second says the buyer's claim was upheld, and the ledger's
`disputed` counter and `dispute_rate_bps` — which have existed and been
unreachable since they were written — finally move.

**The frontend gains a signing step.** 4.05 must request a challenge, have the
wallet sign the returned message and post the signature; 4.06 reads the dispute
back by its id. Neither can be built against the capability token that the card
originally suggested and that the frontend already holds.

**Later stories inherit two frozen numbers.** A dispute carries the charge and
the creditable amount as they stood when it was opened, so 4.03 pays from the
record rather than recomputing from config, and a tuning change between opening
and adjudication cannot alter what the buyer was shown.

Related: ADR 0002 (the refund mechanism and the trust model), `docs/disputes.md`
(the operator- and buyer-facing guide), `app/services/dispute_store.py` and
`app/services/refund_svc.py`.
