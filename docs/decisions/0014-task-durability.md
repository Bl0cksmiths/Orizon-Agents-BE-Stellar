# ADR 0014 — Tasks, traces and plans survive a restart

- **Status:** Accepted, 2026-10-06
- **Deciders:** Danielle (lead)
- **Fixes** QA defect D-090. **Builds on** ADR 0003 (Neon Postgres for durable
  state) and ADR 0007 (the settlement record a receipt reads).

## Context

`AppState` held every task, trace, read token and stored plan in process
memory. Render's free instance restarts whenever it idles (and on every
deploy), so a receipt link a buyer was handed — `GET /api/tasks/{id}` —
answered `404 unknown_task` after each restart, for a run that had settled and
been paid for. The settlement itself survived in `workflow_settlements`; the
task, its trace, its artifact and its read token did not. A plan built just
before a restart was lost the same way, and on the paid path `/execute`
answered that by releasing the buyer's custody as `plan_unknown`.

## Decision

### D1. AppState stays the hot cache; Postgres becomes the record

`services/task_store.py` adds three tables beside the dispute store's, created
by idempotent DDL on first use like every other store in the service:

| Table | Key | Write |
| --- | --- | --- |
| `task_records` | `task_id` | upsert of the latest snapshot; the read token as its SHA-256 digest only, COALESCEd so later snapshots never erase it |
| `task_trace_lines` | `(task_id, seq)` | insert, `ON CONFLICT DO NOTHING` |
| `stored_plans` | `plan_id` | upsert |

Indexes: the primary keys serve every read (one task, its trace in `seq`
order, one plan); `task_records (started_at DESC)` and
`stored_plans (created_at)` serve retention.

Bodies are ASCII-escaped JSON in `TEXT`, not `JSONB`: an artifact is operator-
or model-written text, and `JSONB` refuses `\u0000` and lone surrogates.

### D2. Write-behind, write-through where a receipt depends on it

Every AppState write reports itself to `services/task_persistence.py`, which
queues it and returns; one background worker per process writes coalesced
batches (latest task snapshot wins) in one transaction. A failed batch is
retried with exponential backoff (0.5 s to 30 s); a row the database refuses
as data is dropped alone, at ERROR. The queue is bounded (1,000 task
snapshots, 20,000 trace lines, 1,000 plans) so an outage cannot grow the
process without limit.

`/decompose` and `/execute` wait, at most 2 s, for the plan or task (and read
token) they hand out to be durable before answering; a finished run waits at
most 5 s for its terminal state. Past either bound the write stays queued.

### D3. Reads fall back to the store, and never hang

A `{task_id}` read the cache cannot serve is read from the store before its
token is checked (`task_auth.require_task_read`). The read is bounded at 6 s;
a store that cannot answer is `503 task_store_unavailable` with
`Retry-After: 5`, never a 404 that would tell a buyer their run does not
exist. A plan read the store cannot answer at `/execute` is
`503 plan_store_unavailable`, and releases nothing. Misses are cached for 5 s,
and ids this service could not have minted are never looked up.

Read-token semantics are unchanged: the same token admits the same reads, a
wrong or missing one is the same 404, and the operator key reads everything.
The token now survives a restart, checked against its stored digest.

### D4. A run another process left unfinished

A task is run by exactly one process. One found non-terminal with another
process's boot id is re-read at most every 2 s (Render overlaps two instances
during a deploy). One with no write for 10 minutes — longer than any run's
longest silence — died with its process: it is closed as `failed` with the
trace line "workflow interrupted — the backend restarted before this run
finished", and that correction is written back.

### D5. Retention

| Record | Kept |
| --- | --- |
| task, with its trace | 30 days, and at most the newest 2,000 tasks |
| plan | 1 day (or twice `PLAN_TTL_SECONDS`, if longer) |

The worker prunes at most once an hour. 2,000 tasks with full artifacts is
about 140 MB before Postgres compresses it, well inside a free Neon branch.
Settlements and disputes are money records and are not pruned here.

## Consequences

- With `DATABASE_URL` unset nothing changes: AppState is the only copy, as
  before, and a WARNING says so once.
- A run cancelled by the shutdown drain flushes its own final state (its
  `finally` awaits the flush, shielded, inside the drain's window). Writes not
  tied to a run or a response — a dispute's trace line, an interrupted run's
  correction — need the lifespan shutdown to flush the queue too
  (`await task_persistence.close()` after the drain in `app/main.py`).
- Writes cost one small transaction per burst, off the request path.
