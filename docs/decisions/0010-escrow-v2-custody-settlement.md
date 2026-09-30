# ADR 0010 — Escrow v2: custody at authorize, one settle that pays each delivered step

- **Status:** Accepted (story 5.01 / BLO-35), 2026-09-28
- **Deciders:** Danielle (lead)
- **Builds on** the frozen PaymentEscrow v2 interface
  (`Contracts-2026/…/docs/escrow-v2-interface.md`, as amended at `cfa2ca4`) and
  **supersedes**, on a v2 deployment only, the single-total `charge` that
  `execution_svc._settle_onchain` submits. ADR 0002's settler-funded refunds,
  ADR 0007's window and ADR 0009's ratings are unchanged. This decides **how
  the backend knows which escrow it has**, **what it pays**, **what it records**,
  **what it does when it cannot pay**, and **how long an authorization must live**.

> **Amended 2026-09-30: deployed.** This ADR was written while testnet still
> settled through v1. Escrow v2 was deployed to testnet on 2026-09-30 with
> `make deploy-escrow-v2`, as
> `CCNO5TENCK3EK532I3OZLZ63323FEEULPAKJ74CUP3JZK3XQINRQ5VC4`; the script read
> back `version()` 2, `settler()` `GDB4N25…CDHP` (the platform's signing key)
> and `admin()` `GA7AI5…5OQV`. The address book took it as contracts #6, the
> frontend pinned it in #101, and Render's `STELLAR_PAYMENT_ESCROW` names it,
> so `/readiness` reports `escrow.version` 2. The first v2 settles landed the
> same day in disclosed team runs, each paying the delivered step's owner and
> each sealed:
> [`f0674419…8d1235`](https://stellar.expert/explorer/testnet/tx/f0674419992bdf30cf730139e54e4cdd985e32b43ee15c91733e08424a8d1235),
> [`19f3420d…a83397`](https://stellar.expert/explorer/testnet/tx/19f3420ddb5232a8328c66ec57c1e34890d09a38350e172fdfd9ce8d04a83397)
> and
> [`785428bf…ca554b`](https://stellar.expert/explorer/testnet/tx/785428bf6552208750b375703556c534da557dccd64df8d1db7f954a04ca554b).
> The owner paid in all three was the team's admin key, so none is an
> external settlement under D10. v1 (`CBJPTMAP…25PI`) stays valid, and its
> history is still counted. Nothing decided here changes.

## Context

PaymentEscrow v1 can never settle (defect D-039): `charge` moves the payer's
funds with only the settler's signature, the settler is fixed with no setter,
and a plan-level `orizon_batch` authorization pays `owner_of(orizon_batch)` —
the settler itself. v2 takes the payer's funds into **custody** at `authorize`
(same signature), and the settler's `settle(caller, auth_id, job_id, payouts)`
pays `owner_of(agent_id)` for each `Payout{agent_id, amount}`, returns the rest
to the payer and emits a v1-shaped `charged` event per payout. `charge` and
`revoke` are gone, `reclaim` lets the payer take unsettled custody back after
`expires_at`, and `version()` answers 2.

Three facts proven on testnet shaped the decisions below (read-only, 2026-09-28):

1. `version()` on the v1 escrow answers `HostError: Error(WasmVm,
   MissingValue)` with the diagnostic "trying to invoke non-existent contract
   function".
2. The registry holds none of the seeded `agt_*` catalogue (`list_ids` has 12
   ids, none `agt_*`), and a payout naming an agent it does not hold makes the
   `owner_of` cross-call trap and **reverts the whole settle** (interface
   amendment, finding S1).
3. The planner's `total_usdc` was `round(total, 4)`, which the console signs as
   the cap; per-step i128 payouts could sum past it (370000 against 370200) and
   the settle would revert `Insufficient` (finding S6).

## Decisions

### D1 — The escrow says which version it is, and the answer is cached

`client.escrow_version(contract_id)` simulates `version()`. A number is the
answer; the host's missing-function error (head AND cause, both required) is
**1**. Every definite answer is cached per contract id for the life of the
process — a new escrow is a new id, never an in-place upgrade. Anything else (an
RPC failure, another simulation error, a non-positive or non-integer result)
raises and is **not** cached.

`execution_svc._escrow_version()` reads an unreadable version as **1**, uncached,
so the next run asks again. That is what "safe" means here: on a v1 escrow it is
today's path exactly; on a v2 escrow the v1 `charge` does not exist, so its
simulation is refused before anything is signed or sent — the run is marked
`failed`, no money moves, the custody stays reclaimable. No v2 settle is ever
sent on a guess, because every v2-only check runs on a definite 2.

`_settle_and_record` dispatches on it: v1 runs `_settle_onchain` unchanged — the
charge's arguments are pinned by a golden test — and v2 runs `_settle_v2`.

### D2 — Payouts are the delivered steps, each at its own price, and nothing else

`_payout_plan` iterates the run loop's own `delivered_steps` — the steps that
produced usable output and moved `spent` — so a step that timed out, raised,
returned the wrong shape or was never dispatched is not paid (5.01 AC5). Each
amount is `usdc_to_i128(step.est_price_usdc)`.

A delivered step is still left out, its share returned to the buyer with the
remainder, and the reason recorded (`SettlementStep.unpaid_reason`), when:

- **`free`** — the contract refuses a zero payout;
- **`no_onchain_owner`** — `AgentRegistry.owner_of` answers NotFound. This is
  every seeded `agt_*` worker today. Naming one would revert every other
  operator's pay with it. Owner reads are cached per agent id: 900 s for an
  owner (written at registration, no transfer entrypoint), 60 s for "none" (it
  ends the moment the operator registers), failures never;
- **`owner_unreadable`** — the owner read failed. Only a **confirmed** owner is
  named, on the same ground;
- **`over_authorized_cap`** — see below.

**The cap.** The payouts are held under the authorization's own `max_amount`
(passed from `/execute`, which read it, or read back at settle). Steps are paid
in plan order; the one that crosses the cap is cut to what is left and any after
it are unpaid. A cut is logged at ERROR and traced — it means the plan priced
more than the buyer authorized, which S6's fix (D8) makes unreachable from a
planned run. `MAX_CHARGE_USDC` still **refuses** an over-cap total, as it does
for v1.

**More than 16.** `settle` takes at most 16 payouts. Up to 16 paid steps get one
payout each, so every step has a receipt of its own. Past that, payouts merge
per agent in first-seen order — each step still recorded at its own amount,
sharing its agent's receipt — and more than 16 distinct agents is refused
whole (and the custody released, D6) rather than paid in part. The planner caps
a plan at six steps, so neither case is reachable from a planned run today.

### D3 — Nothing delivered: release under v2, skip under v1

v2: `settle` with an empty `payouts` returns the whole custody now, instead of
leaving it locked until the buyer reclaims after expiry. Nothing is recorded or
sealed. Its job id is `unsettled_job_id(task_id)`, the id the run's ratings are
written under. v1: unchanged — nothing is charged, the authorization is left.

### D4 — Receipts go into the seal, one per payout

`settle` returns `Vec<BytesN<16>>`, which `_finalize_invoke` hands over as a
list of raw bytes; `client.receipt_ids` decodes it (hex strings accepted),
all-or-nothing. The seal gets every receipt in `payouts` order and
`total_spent = Σ payouts`, not the plan's estimate. A result that does not
decode to exactly one id per payout is logged, and the seal goes out without
receipt links, as v1 already does for an undecodable receipt.

### D5 — The settlement keeps what each step was actually paid

`SettlementStep` gains `paid_usdc`, `receipt_id_hex` and `unpaid_reason`, all
`None` on a v1 record. On a v2 record a **delivered** step's `price_usdc` is its
payout — the number every credit is computed from — so the dispute path
(`dispute_svc`, `refund_svc.creditable_for`, the receipt's `creditable_usdc`)
stays correct with no change: an unpaid step is at 0.0 and is refused as
`nothing_was_charged`. An undelivered step keeps the plan's quote and 0.0 paid.
`settled_usdc` is `Σ payouts`.

The fields live in the existing JSONB `steps` column, so **the table's shape
does not change and no migration statement runs**. Rows written before this ADR
lack the keys and read back as v1 records (pinned against a real Postgres). The
in-memory store holds the dataclass itself, so parity is automatic. The receipt
(`GET /api/tasks/{id}/disputes`) exposes all three per step.

### D6 — Unknown outcomes are never retried, only confirmed hashes are kept, and custody is never stranded

**Unconfirmed.** A settle that times out, or whose send or poll failed after it
may have reached the network (`InFlightError`), may still land. Exactly as for
v1's charge: it is reported `unconfirmed`, no settlement is recorded, and it is
**never retried** — nor released, which would be a second settle of the same
authorization.

**Hashes (S7).** `charge_tx`/`proof_tx` — on the task and on the settlement — are
set only for a transaction that CONFIRMED, on both versions. A rejected or
unconfirmed hash stays in the log line and the trace line, never on a field a
reader takes as evidence.

**Expiry.** The amended interface does **not** refuse a settle past
`expires_at`; from then on the payer may `reclaim`, and whichever lands first
wins. So the settle is always attempted. Losing the race comes back `Revoked`
(or `Replay`), reported as "not settled by us" — nothing moved, nothing to release.

**Release (S4).** `release_authorization(auth_id_hex, *, reason) -> str | None`
is the one way to hand v2 custody back; its signature is frozen for the other
lanes. It submits `settle(settler, auth_id, <fresh job id>, [])`, returns the
hash only when the release confirmed, is a no-op on v1, never raises, logs every
outcome and never retries an unknown one. Inside the run it is called when:

- the run is cancelled or killed by shutdown before any settle was handed to the chain;
- the run fails on an unexpected error before any settle was handed to the chain;
- the settle is refused before it is built (payouts, cap, unreadable authorization);
- the settle is refused before it is sent (`NotSubmittedError`: simulation, signer, RPC refusal).
- the ledger rejects the settle (`FAILED`): definitive, nothing moved.

Once a settle has been handed to the chain, the run never releases: that settle
may still land.

### D7 — `/execute` refuses a v2 authorization that cannot pay for the run

Before any task is minted (and before the capacity check, so nothing awaits
between counting a slot and taking it), a v2 authorization is read and refused
when it is unreadable (503), missing (404), made by another wallet (403 — the
payer is who a dispute credit goes to), made for another plan (409, the label
is the plan id), already settled or reclaimed (409), smaller than the plan's
total (409), or **expiring before a worst-case run could settle** (409
`authorization_expiring`). v1 reads nothing and refuses nothing.

**The number.** `worst_case_run_seconds(n)` = the re-check's batch read
(`REPUTATION_BATCH_TIMEOUT_SECONDS`, 2.5 s shipped) + n × (120 s step ceiling +
5 s of lookups and trace writes) + 150 s for the settle (load, prepare and send
at up to 15 s × 2 attempts each, the transaction's 30 s validity window, the
owner reads). These are ceilings the code already enforces, not measurements.

| steps | worst case |
|---|---|
| 1 | 277.5 s |
| 3 | 527.5 s |
| 4 | 652.5 s |
| 6 (the planner's maximum) | 902.5 s |

The console's `ttl_seconds: 600` covers three steps. **Recommended: 1200 s**,
which covers a six-step run with about five minutes for the wallet signature
and the authorize to confirm, inside the route's `le=3600`. A longer TTL costs
the buyer nothing on the happy path — the settle returns the remainder at once —
and only lengthens how long a stranded authorization waits for `reclaim`.
`AuthorizeReq`'s default of 300 s predates v2 and covers only a one-step plan.

### D8 — The plan total is exactly what paying every step moves

`orchestrator_svc.authorizable_total_usdc` returns
`Σ usdc_to_i128(price) / 10^7` instead of `round(Σ price, 4)`, so the cap the
console signs is, to the stroop, what the settle would pay for a fully delivered
run; `usdc_to_i128` of it gives the same integer back.

### D9 — The settlement outcome is a field, not a sentence

`SettlementState = "settled" | "released" | "skipped" | "unconfirmed" | "failed"`,
on `TaskSummary.settlement` (null for a simulated run), on the one `TraceLine`
that reports it (`settlement`, null on every other line — written in the same
breath as the task field, so the two cannot disagree), and on the receipt as
`settlement_state` (`settled` when a settlement is on record, otherwise the
task's own while this process holds it). A run whose settlement failed still
finalizes `complete` when it delivered; this field is how the console and the
harness tell. `/readiness` reports the escrow id and its cached version.

### D10 — Operator earnings are per agent, and still only third-party money counts

v2's `charged` topic is the agent actually paid, and its payload is v1's, so
`settlement_svc` decodes it unchanged and per-agent earnings become real. The
payer is still `authorization(auth_id).payer` — now the buyer whose custody
funded the payout. Two premises moved:

- **The settler can rotate.** Besides the escrow's current `settler()`, a
  payout funded by this deployment's own signing key or its admin is excluded
  as `settler` — the platform paying — even when a rotation has not reached
  the cached read. The `owner` rule still outranks it.
- **The escrow id changes.** Earnings are read from the **configured** escrow
  only, so after the switch v1's history drops out of the scan. Nothing is lost:
  every `charged` event v1 ever emitted was the platform paying itself and was
  excluded from revenue. The v1 ids stay in the evidence docs as history.

## Consequences

- **Seeded workers are not paid under v2** until each is registered on-chain
  with an owner. Their steps still run, are rated and are recorded, at 0.0 paid
  and not disputable, and the buyer keeps their share. Registering them (owner
  = a platform treasury) is a product decision this ADR does not take.
- `POST /api/stellar/server/charge` is v1-only; against v2 its simulation is
  refused ("function not found").
- The `build_authorize` docstring still says a v2 settle is refused after
  `expires_at` (written before the amendment); the router now belongs to
  another lane, which owns that correction.
