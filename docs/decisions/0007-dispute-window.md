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
