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
