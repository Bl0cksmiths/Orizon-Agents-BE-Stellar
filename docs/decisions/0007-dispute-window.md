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
`complete`/`failed` tasks from first** — precisely the set a buyer disputes —
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
