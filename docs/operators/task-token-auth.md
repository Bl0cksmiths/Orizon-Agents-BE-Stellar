# Task read tokens: turning on `TASK_AUTH_REQUIRED` safely (proposal)

**Status:** proposal, from an audit on 2026-09-30. Nothing here is changed yet.
`TASK_AUTH_REQUIRED` stays `false` on the testnet deployment, and this document
is the record of why, and of what must ship before it can be turned on.

## Current state

- **The switch.** `TASK_AUTH_REQUIRED` (`app/config.py`, `task_auth_required`)
  defaults to `false`, and the public deployment runs with it off. With it off,
  every task read is open to anyone who has the task id.
- **The token.** `/execute` mints a per-task read token
  (`secrets.token_urlsafe(24)`, `app/services/execution_svc.py`) and returns it
  once, as `read_token` in the execute response
  (`app/routers/orchestrator.py`). It is kept in `state.task_tokens`, an
  in-memory dict in `app/state.py`, and evicted with its task when the
  200-task store is full.
- **What the switch gates.** `require_task_read` (`app/task_auth.py`) guards
  `GET /api/tasks/{id}`, `GET /api/tasks/{id}/artifact`, the trace and the
  trace stream (`app/routers/tasks.py`, `app/routers/trace.py`). With the
  switch on, each needs the token (`X-Task-Token`, or `?token=` for
  EventSource) or the operator `X-API-Key`; anything else answers
  `404 unknown_task`. `GET /api/tasks/{id}/disputes` is gated by the same
  proof. `GET /api/tasks` (the recent-tasks list) answers `[]`.
- **What it does not gate.** A single dispute, `GET /api/disputes/{id}`, stays
  readable; its free text is withheld without a token, an operator key or the
  payer's dispute read grant, whatever the switch says (`docs/disputes.md`).
  Opening a dispute is gated by the payer's wallet signature, never by the
  token.
- **The frontend.** The frontend remembers each token at execute time and
  replays it on task reads (`lib/task-tokens.ts`, `lib/api.ts` in the frontend
  repo). The store is `sessionStorage`: it survives a reload and dies with the
  tab, and it is never shared across tabs.

## What breaks if it is turned on today

1. **A buyer's receipt disappears in a new tab.** The token lives in the tab
   that ran the task. The same buyer opening the trace or receipt in a new tab,
   another browser or another device has no token, and every task read answers
   `404 unknown_task`, as if the task did not exist.
2. **Shared trace links break.** A trace link carries only the task id. The
   person it is shared with has no token, so the page cannot load the task, its
   artifact or its trace. The frontend's dispute receipt is written to serve "a
   stranger on a shared link"; that reader would see nothing.
3. **The recent-tasks list empties.** `GET /api/tasks` returns `[]` whenever
   the switch is on, so every recent-tasks view in the dApp goes blank for
   everyone.
4. **Tokens are lost on restart.** `state.task_tokens` is process memory. A
   Render restart, redeploy or free-tier spin-down forgets every token. The
   settlement and its disputes survive in Postgres, but
   `GET /api/tasks/{id}/disputes` then answers as if the task were unknown, and
   that read is where the console gets the job id to start a **first**
   dispute. So after a restart under enforcement, a buyer inside a valid 24-hour
   dispute window cannot open one from the console (`docs/disputes.md`,
   "For operators: where the records live"; ADR 0007, "Why not turn
   `TASK_AUTH_REQUIRED` on and keep the token").

Any one of these would be a visible regression on the public demo; the fourth
also takes a right away from paying buyers.

## The safe path

Each item removes one breakage above. All four are needed before the switch
is turned on.

1. **Keep the receipt reachable.** The payer must be able to reach their own
   receipt without the tab that ran it. The dispute read grant (D-067) already
   proves "I am the wallet that paid this task" by a signed challenge; the
   receipt and the disputes-on-task read should accept that same proof, so a
   payer in a new tab or on another device signs once and reads.
2. **Put the token in the share-link fragment.** A shared trace link becomes
   `…/app/trace?task=<id>#t=<token>`. The fragment is never sent to a server,
   so it stays out of access logs and `Referer` headers; the page reads it and
   sends it as `X-Task-Token`. Sharing then stays a deliberate act by someone
   who holds the token.
3. **Make tokens outlive the tab and the process.** Two options:
   - persist tokens beside the task records (Postgres), with the same lifetime
     as the settlement and its dispute window; or
   - **derive** them instead of storing them: `token = HMAC(server key, task_id)`,
     checked in constant time. Nothing to store, nothing lost on restart, and
     rotating the key revokes every token at once. This is the smaller change.
   On the frontend, keep tokens longer than the tab (for example `localStorage`
   with a bounded size), or rely on item 1 for the payer.
4. **Scope the recent-tasks list** rather than empty it: list only the tasks
   the caller proves they may read (their own wallet's, or those whose tokens
   they send), or drop the global list from the public dApp before the switch.

## Staging the switch

1. **Ship the pieces with the switch off.** Items 1 to 4 above, each behind no
   flag, each harmless while enforcement is off. The frontend sends tokens it
   already has; the backend accepts them and ignores their absence.
2. **Measure.** With the switch still off, log (at INFO, without the token)
   each task read that arrives *without* a valid token, by route. That count is
   what the switch would refuse. Watch it for a few days of normal traffic.
3. **Turn it on on a preview or staging deployment first.** Run the frontend's
   e2e suite and one wallet-authorized lifecycle run (`scripts.lifecycle`)
   against it, including a new-tab receipt, a shared link, a restart between
   seal and dispute (5.01 AC4), and a first dispute after that restart.
4. **Turn it on in production** by setting `TASK_AUTH_REQUIRED=true` in the
   Render dashboard and a Manual Deploy. Check `/readiness`, the trace of a new
   run in a new tab, and a shared link.
5. **Rollback:** set it back to `false` and Manual Deploy. Nothing is lost:
   the switch changes who may read, not what is stored.

Until step 4, the switch stays off, and task reads stay public by design, as
the README's "Public-demo scope" note says.
