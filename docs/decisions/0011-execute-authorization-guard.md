# ADR 0011 — The execute authorization guard: one verifier, one task per authorization, and custody back on a refusal

- **Status:** Accepted (story 5.01 / BLO-35, seam audit finding S2), 2026-09-28
- **Deciders:** Danielle (lead)
- **Builds on** ADR 0010, which moved payment to PaymentEscrow v2 (custody at
  `authorize`, per-step `settle`, the payer's `reclaim`) and put the check that
  an authorization may pay for a run inside `execution_svc.execute_plan`
  (`_authorize_for_execute`). This decides **what the `/execute` route adds
  around that check**, **how one authorization is kept to one task**, **when a
  refused execute hands the buyer's custody back**, **how a payer reclaims it
  themselves**, and **what a simulated run may write on-chain**.

## Context

### 1. `/execute` took any authorization on trust (S2)

`POST /api/orchestrator/execute` accepted any `auth_id_hex` and `payer` and
never looked at the chain. Under v2 that is theft, not a bug: `auth_id` and
`payer` are public — they are the data of every `authd` event — so anyone could
point **their own plan** at a victim's authorization, and the first `settle`
would pay the agents the attacker chose, up to the victim's `max_amount`. The
frozen interface closes it by making the authorization's label (the `agent_id`
argument of `authorize`) the **plan id** the buyer is paying for, and requiring
the backend to refuse a plan whose id, payer, cap or state does not match.

The same hole already worked on v1 as **rating farming**: an execute with a
made-up authorization runs the plan, the charge fails (v1 cannot settle at
all), and the ratings are written anyway, because a rating answers "who
delivered", not "who got paid" (ADR 0005 D2).

### 2. Two lanes built the same check

The settle lane (ADR 0010) put the full check in `execute_plan`, where it also
hands the verified `max_amount` to the settle. This lane first built a second
one in the route. Two verifiers meant two chain reads per execute and two sets
of codes for the same condition, so the route's was removed (D1).

### 3. What a simulated run writes

A run with no authorization is the demo path. If it wrote ratings, rating
farming would need no authorization at all, real or fake. It does not (D6).

## Decisions

### D1 — One verifier: `execute_plan`'s

Whether an authorization may pay for a run is decided once, in
`execute_plan`, before any task is minted, and only against a v2 escrow. The
route does not read the authorization before calling it. Its refusals reach
the client unchanged, in the app's error envelope:

| Code | Status | Condition |
|---|---|---|
| `authorization_unreadable` | 503 | the chain could not be read — never a pass |
| `authorization_not_found` | 404 | the escrow answers `NotFound` |
| `authorization_payer_mismatch` | 403 | made by a different wallet |
| `authorization_plan_mismatch` | 409 | made for a different plan — S2 itself |
| `authorization_spent` | 409 | already settled, or reclaimed by the payer |
| `authorization_insufficient` | 409 | `max_amount` below the per-step payouts |
| `authorization_expiring` | 409 | expires before a worst-case run could settle |

Every refusal this lane makes itself uses the same code for the same
condition, so a client learns one vocabulary.

### D2 — The escrow version is read, never guessed, before a paid run

`execute_plan` reads an **unreadable** version as v1 and then skips its check;
and a v2 settle whose run was never checked re-reads the authorization for its
cap but not its payer or label. So a paid execute that reached `execute_plan`
before any version had been read — a cold start with the RPC failing — could
run an attacker's plan unchecked and settle it against the victim's custody
once the RPC recovered.

The route therefore reads the version itself, before a paid run, and answers
**503 `authorization_unreadable`** when it cannot. It reads through the
client's `escrow_version` when the build has it, so the answer lands in the
same per-contract cache `execute_plan` reads, and `execute_plan` can never see
an unread version on this path. A definite answer is cached for the life of
the process (a new escrow is a new contract id), so this costs one read per
process, not one per execute. An unconfigured escrow is also a 503, not v1.

### D3 — One authorization, one task

A v2 authorization is custody for one run: the first `settle` spends it and
every later one is a `Replay`. A second execute against it would run a whole
plan nobody can pay for, and while the first is still running it would race
the first run's settle.

- **The claim.** On a v2 escrow, the route claims the authorization
  (`PENDING`) before calling `execute_plan`, records the task id when one is
  minted, and drops the claim if `execute_plan` raises first — whatever it
  raised, so a buyer refused for a fixable reason can retry the same
  authorization. A second execute against a claimed authorization is **409
  `authorization_used`**, whether its task is running or finished.
- **Atomic in one process.** Everything from the claim check to the claim runs
  under an `asyncio.Lock` keyed by the authorization id. Without it, two
  executes both pass the check across the version read's `await` and both run
  (the concurrency test proves it). Executes against different authorizations
  never wait on each other, and a lock is dropped when its last waiter leaves.
- **Bounded.** At most 4 096 claims; a pending or running one is never the one
  evicted. A claim outlives its request only when a v2 run was minted, which
  `execute_plan` allows only for custody the payer really locked.
- **The durable half is the chain.** A run that settled left the authorization
  `settled`; one the payer reclaimed left it `revoked`; `execute_plan` refuses
  both as `authorization_spent`, after a restart included. The settlement
  record in the dispute store (`SettlementRecord.auth_id_hex`) is written only
  when that same settle confirms, so it holds nothing the chain read does not
  already refuse — and it has no lookup by authorization id. What only this
  process knows is the window before a settle lands, and the claims cover it.
- **One worker.** The deployment runs `uvicorn --workers 1` (render.yaml), so
  this process is the whole service. A second worker would hold its own claims
  and could start a second run against one authorization. Scaling out needs a
  shared claim (a row with a unique key on the authorization id) first.

Against a v1 escrow nothing is claimed: v1 holds no custody, and its behaviour
is unchanged (see D6 for what that leaves open).

### D4 — Custody goes back when the route refuses a run that can never happen

A buyer who authorized and was then refused has their `max_amount` locked in
the escrow until `expires_at`, and must then sign a `reclaim`. For three
refusals the route makes before any run, it hands the custody back at once
through the settle lane's `execution_svc.release_authorization` — a `settle`
with no payouts, which returns every stroop to the payer:

| Refusal | Status | Why the run can never happen |
|---|---|---|
| unknown plan (a restart, or evicted) | 404 | the label is this plan id; it can pay for no other |
| plan expired | 410 | the same |
| no capacity | 503 | the service refused the run |

Rules:

- **Ownership first.** It releases only an authorization it has just read and
  found live, made by this payer, and labelled for this plan. The cap and the
  expiry are not checked — a release needs neither. An authorization that
  fails ownership is never released: it may be someone else's custody, and
  whoever sent its public id has no say over it.
- **Never under a run.** The claim check comes first, so an authorization that
  is funding a run — even one whose plan has since been evicted — is refused
  as `authorization_used` and never released.
- **Never for `execute_plan`'s own refusals** (D1): that authorization may be
  someone else's, or another plan's.
- **Never raises.** `release_authorization` never raises by contract, and the
  route guards it anyway; it is looked up at call time, so a build without it
  answers the refusal with no release.
- **The client is told.** When a release was attempted, the error body carries
  one extra top-level field, `release_tx_hash`: the release's transaction hash
  when it confirmed ("your funds were returned"), or `null` when it did not
  (the funds stay reclaimable after expiry). A refusal that attempted nothing
  keeps exactly the envelope it had.

Capacity is listed although the buyer could, in principle, retry the same plan
once a slot frees. The release was chosen because a refused buyer otherwise
waits out the whole TTL (D7) to get their money back; the cost is that a
retry needs a fresh authorization.

### D5 — `POST /api/stellar/build/reclaim`

The payer's own way back to custody no settle ever spent. The contract is
frozen and the frontend codes against it: body `{payer: "G…", auth_id_hex: 32
lowercase hex}`, answer `{xdr}` — an unsigned, prepared `reclaim(payer,
auth_id)` with the payer as source, built exactly as `build/authorize` builds
(any build failure is 400 `build_failed`). It is rate-limited and validated
like its siblings: the service-wide limiter and the body patterns.

Before building, a read-only simulate checks what `reclaim` itself would
check, in its order, so the wallet is never asked to sign a transaction the
contract will refuse:

| Code | Status | Condition |
|---|---|---|
| `reclaim_unsupported` | 409 | the escrow is v1, which holds no custody |
| `authorization_not_found` | 404 | no such authorization |
| `authorization_payer_mismatch` | 403 | only the payer can reclaim (`Unauthorized`) |
| `authorization_spent` | 409 | already settled (`Replay`) or reclaimed (`Revoked`); the message says which |
| `authorization_locked` | 409 | not yet expired (`Locked`); the message gives `expires_at` |
| `authorization_unreadable` | 503 | the chain could not be read |

`reclaim` compares against the ledger's clock, which trails the wall clock by
up to a close, so the route waits 10 s past `expires_at` before it will build.

### D6 — A simulated run writes nothing on-chain

Checked, not assumed. `execute_plan` submits ratings, charges and seals only
on the paid path (`if auth_id_hex and payer`); a simulated run's "payment" and
"attestation" are trace lines, and its only other effects are off-chain (the
operators it dispatches to, and a log-only failure counter). A test runs a real
simulated workflow on a deployment that could rate — signing key and ledger
configured — and asserts that no rating, charge or seal is attempted; the same
run, paid against v1 with a made-up authorization, rates every step, which is
what gives the first assertion its teeth and records that v1 farming is still
open. No change to `execution_svc` was needed.

That v1 hole closes when the configured escrow is v2. Until then a v1 paid
execute is unchecked by design: v1 cannot settle, and refusing its runs would
only cost the live demo.

A request that sends only one of `auth_id_hex` and `payer` used to run
simulated without a word; it is now **422 `authorization_incomplete`**.

### D7 — The two v1 leftovers in the stellar router

- **`build/authorize` defaults to a 1 800 s TTL** (was 300; the bound stays
  3 600). On v2, `execute_plan` refuses an authorization that cannot outlive a
  worst-case run of its plan — 902.5 s for a six-step plan — and the TTL starts
  before the wallet prompt, the authorize's confirmation and the execute. 300 s
  covered only a one-step plan signed at once. The price of longer is that a
  payer can reclaim only after expiry, which D4 softens.
- **`/server/charge` is v1 only.** Against a v2 escrow it answers 409
  `charge_unsupported_on_v2` and says a v2 run is paid by `settle`. An
  unreadable version is let through to the contract, which is safe here:
  nothing is refused on the answer, and v2 has no `charge` for the simulation
  to find.

## Consequences and residual risks

- **Front-running with the victim's own triple.** The route cannot tell the
  payer from anyone who read their `authd` event. An attacker who sends a
  victim's `(auth_id, payer, plan_id)` before the victim does starts the
  victim's own plan, paid by the victim; the attacker holds the task id and
  its read token, and the victim's execute is `authorization_used`. No money
  moves to the attacker, but the victim loses their result. Closing it needs
  proof of the payer at execute — a signature over the plan id, or a secret
  capability `decompose` returns only to its caller — which is a schema and
  frontend change, out of this story.
- **Release griefing.** The same observer can trigger D4's release on an
  unknown or expired plan, or while capacity is exhausted. The funds go back to
  the payer, never elsewhere; the cost is a fresh authorization.
- **One worker** (D3).
- **v1 rating farming** stays open until the switch to v2 (D6).
