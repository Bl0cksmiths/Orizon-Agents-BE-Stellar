# ADR 0015 — Payment matches the decomposer's prices, to the stroop

- **Status:** Accepted, 2026-10-06
- **Deciders:** Danielle (lead)
- **Builds on** ADR 0010 (escrow v2: custody at authorize, one settle that pays
  each delivered step) and ADR 0011 (the `/execute` authorization guard).
  **Amends** ADR 0010 D5 (the settlement record gains the plan's price per step
  and the authorization) and ADR 0002 (a dispute credit is rounded down).

## Context

The owner's rule for the agent-pipelines sprint: what the plan shows per step
and in total is what the buyer authorizes, and what is settled per delivered
step plus what is returned is that authorization — to the stroop.

Money on this platform is an i128 in the escrow token's smallest unit. A
read-only probe of testnet on 2026-10-06 (simulated views and one
`getLedgerEntries`, no transaction) established the units:

| Read | Answer |
| --- | --- |
| escrow v2 `CCNO5TEN…5VC4` `version()` | `2` |
| its instance storage `Usdc` | `CDLZFC3S…CYSC` |
| `Asset.native().contract_id(testnet passphrase)` | `CDLZFC3S…CYSC` — the same id |
| that SAC's `name()` / `symbol()` / `decimals()` | `native` / `native` / `7` |
| its instance storage `Registry` | `CAPHXWU5…BJ3GQ` |
| that registry's `owner_of` for `agt_01h8`, `agt_11c0`, `agt_12r0` | `Error(Contract, #2)` — NotFound |

So the escrow takes custody in **native XLM**, 7 decimals; one stroop is
10⁻⁷ XLM. The contract (`contract/payment-escrow/src/lib.rs`, origin/main)
sums the payouts, refuses `sum > max_amount` (`Insufficient`), pays each one
to `owner_of(agent_id)` and returns `max_amount − sum` to the payer in the same
transaction, emitting `settled(auth_id, job_id, sum, returned)`. The chain
therefore guarantees `settled + returned = authorized` for whatever we ask it
to pay; this ADR is about asking it for exactly the right numbers.

## The money path, as audited

Every place a price became an amount, before this ADR:

| # | Where | What it did | Unit / rounding / label |
| --- | --- | --- | --- |
| 1 | `seed.py`, `registry_sync` | Agent price: seed literal (3 dp), or on-chain i128 `/ 1e7` | float; exact for ≤ 7 dp |
| 2 | `orchestrator_svc._kit_step`, `_clamp`, fallback | `PlanStep.est_price_usdc = agent.price` | float, never rounded |
| 3 | `orchestrator_svc` kit and free-form | `StoredPlan.total_usdc = Σ est_price_usdc` | **float sum** |
| 4 | `orchestrator_svc.authorizable_total_usdc` | `DecomposeResponse.total_usdc = Σ round(p·10⁷) / 10⁷` | float of a stroop sum |
| 5 | planner prompt (`render_agents_block`) | `price={a.price:.3f}` | 3 dp |
| 6 | `/api/stellar/build/authorize` | `usdc_to_i128(max_amount_usdc)` = `round(x·10⁷)` | any float, unchecked |
| 7 | `execution_svc._authorize_for_execute` | `Σ usdc_to_i128(est_price_usdc) > max_amount` → 409 | stroops |
| 8 | `execution_svc._run` | `spent += est_price_usdc`; `Task.spent = round(spent, 4)` | float, 4 dp, on paid runs too |
| 9 | `execution_svc._run` (simulated) | trace `… :: {p:.3f} USDC (simulated)` | 3 dp, **USDC** |
| 10 | `execution_svc._payout_plan` | payout = `usdc_to_i128(est_price_usdc)` | stroops |
| 11 | `execution_svc._settle_v2` | trace `x402 settle → {t:.3f} USDC paid …` | 3 dp, **USDC** |
| 12 | `execution_svc._sealed`, v1 `_settle_onchain` | trace `workflow sealed — … {t:.3f} USDC` | 3 dp, **USDC** |
| 13 | `execution_svc._v2_settlement_record` | delivered step `price_usdc = paid`; undelivered keeps quote | float; planned price of a delivered **unpaid** step lost; authorization not kept |
| 14 | `routers/disputes` receipt | `price_usdc`, `paid_usdc`, `settled_usdc` | floats; no planned / returned / authorized |
| 15 | `refund_svc.credited_amount_usdc` (dispute_svc ~561, receipt) | `round(charged · fraction, 7)` | float, **round to nearest** |
| 16 | `dispute_svc._note_credit_on_workflow` | trace `credited {x:.7f} USDC` | **USDC** |
| 17 | `reputation_svc.rating_weight_stroops` | `round(min(price, cap) · 10⁷)` | stroops, from the plan's float |
| 18 | `routers/payments` (simulated x402) | `amount={x:.3f};token=USDC` | 3 dp, **USDC** |
| 19 | `routers/stellar /network` | `asset="native"` literal | right on testnet by accident |

## Discrepancies found

Each was pinned by a failing test, committed before its fix.

**P1 — The stored plan's total was not the total the buyer was shown.** Row 3
summed floats: copywrite.v3 + seo.brief stored `0.020999999999999998` while the
response said `0.021`. `/execute` and every later read judge the run by the
stored plan. Test `test_the_stored_plan_total_is_the_total_the_buyer_was_shown`
(8f42781) → fix 55d5e0c.

**P2 — The authorization amount was never checked against the plan.** Row 6
built whatever float it was sent. The console sends `plan.total_usdc || 0.001`,
so a zero total authorizes 0.001; anything above the total locks custody no
step can be paid from for the life of the run; anything below is only refused
by `/execute` after the wallet has already moved it into custody. Test
`test_an_amount_that_is_not_the_held_plans_total_is_refused` (ecb33ec) → fix
b9f6737.

**P3 — `Task.spent` claimed money that did not move.** Row 8 rounded the
delivered steps' estimates to 4 dp and reported them on paid runs too: a v2 run
whose seeded step had no on-chain owner showed `spent 0.032` while its settle
paid `0.02`; a simulated `0.0123457 + 0.00001` showed `0.0124`. Tests
`test_the_task_says_what_was_charged_not_what_was_delivered`,
`test_a_simulated_run_spends_the_delivered_prices_exactly` (83e2f6e) → fix
a5fd2ab.

**P4 — The receipt could not reconcile.** Row 13 overwrote a delivered-but-
unpaid step's price with the 0.0 it was paid, and nothing kept the
authorization, so no receipt could show planned / charged / returned or prove
`settled + returned = authorized`. Tests in `test_exact_settlement.py` (83e2f6e)
→ fixes 732b48a (record), a5fd2ab (writer), 69eadd3 (receipt).

**P5 — The native asset was labelled "USDC", and amounts were cut to 3 dp.**
Rows 9, 11, 12, 16, 18: a 0.0125 step traced as `0.013 USDC` (or `0.012`,
binary rounding), on an escrow moving XLM. Row 19 would have said `native` for
any SAC. Tests `test_a_paid_runs_trace_names_the_asset_and_the_exact_amount`,
`test_a_simulated_runs_trace_names_…` (83e2f6e), `test_the_credit_line_…`
(18a2937), `test_x402_challenge…`, `test_the_network_names_the_asset_its_sac_wraps`
(7fb07d3) → fixes d26ae48, 450ccb4, 3321258, 5ab5ee6.

**P6 — A dispute credit could exceed its policy share by a stroop.** Row 15
rounded to nearest: 90% of a 2-stroop charge credited all 2; ⅓ of 0.0812534
credited 270 845 stroops, one above the share. Test
`test_a_credit_is_the_share_rounded_down_to_the_stroop` and the random
settlement sweep (4a7ad65) → fix 8e37c87.

**P7 — Two conversion rules.** Row 6/10's `usdc_to_i128` rounded the binary
double: `0.00000125` became 13 stroops and `0.00000455` became 45 — one up, one
down — where the written decimals round half-to-even to 12 and 46. No price
the platform holds has more than 7 dp, so no plan was affected, but the authorize
route and the register route take arbitrary floats. Test
`test_the_ledger_client_converts_by_the_same_one_rule` (d71de59) → fix f2f83f9.

### Found, and not fixable in code

**P8 — No platform agent can be paid on the live escrow.** The registry the
escrow reads holds no `agt_*` id (table above), and ADR 0010 D2 leaves such a
step unpaid rather than revert every other payout. With plans now routed to
in-platform agents only (`PLANNER_ROUTE_EXTERNAL` false), **every paid run
returns its whole authorization**: `settled + returned = authorized` holds,
and every step's receipt says planned P, charged 0, returned P,
`unpaid_reason: no_onchain_owner`. "Each delivered step settles its price"
becomes true for platform agents only once each `agt_*` id is registered
on-chain with an owner (a platform treasury) — an on-chain transaction and a
product decision, proposed to the owner, not taken here.

**P9 — Out of this lane's files.** The planner is shown prices at 3 dp (row 5,
planner lane: the clamp re-prices from the registry, so no charge is affected,
but a 0.0125 agent reads as 0.013 to the model). The console authorizes
`plan.total_usdc || 0.001` and shows `toFixed(3)` (FE lane: sign
`total_stroops`; display `display` strings).

### Checked, and sound

- **Prices are frozen at plan time.** Nothing after the clamp reads
  `agent.price`; execution uses the stored step. Now pinned:
  `test_a_registry_price_change_after_planning_changes_nothing`.
- **Expired plans re-plan.** `/execute` refuses a plan older than
  `PLAN_TTL_SECONDS` (900 s) with 410 `plan_expired` and releases the custody
  (ADR 0011); the buyer plans again at current prices.
- **The registry's float round-trips.** `raw / 1e7` and back is exact for every
  i128 an operator can register (20 000 random samples in `test_money.py`).
- **The payout cap.** `_payout_plan` clamps to `max_amount`; with the
  authorization equal to the plan's total it never bites.
- **Rating weights** are the step's planned price in stroops, capped (row 17);
  the plan's float now converts exactly, so the weight is the plan's price.

## Decision

### D1 — One helper, integer stroops, `Decimal` only

`app/money.py` is the only conversion: `to_stroops` (from the float's shortest
repr through `Decimal`, half-to-even past 7 dp, refusing non-finite, negative
and past-i128 with `MoneyError(ValueError)`), `stroops_to_float` (the double
nearest the exact amount; round-trips), `format_amount` (every decimal that
carries something, never fewer than 3), `fraction_of` (a share, rounded down),
and `current_asset` / `asset_code`. `client.usdc_to_i128` delegates to it.

### D2 — The plan is priced in stroops; floats are derived

`PlanStep.price_stroops` is frozen at plan time from the agent's registry
price; `est_price_usdc` is re-derived from it on every validation (and a plan
stored before this converts once, exactly). `Plan.total_stroops` is computed —
always `Σ price_stroops`. `StoredPlan.total_usdc`, `DecomposeResponse.total_usdc`
and `total_stroops` are derived and cannot disagree. A non-finite or negative
price is refused at the step.

### D3 — The asset comes from the network config

`Plan.asset` / `DecomposeResponse.asset` = `{code, issuer, decimals: 7}`:
`XLM`/null when `STELLAR_ASSET_SAC` is the network's native SAC (derived with
`Asset.native().contract_id`, no read) or unset; otherwise the SAC's `name()`,
read once and remembered; `UNKNOWN` if unreadable — never a guessed "USDC".
Every trace line, the credit line, the x402 challenge and `/network` use it.

### D4 — Authorize exactly the total

`build/authorize` takes `max_amount_stroops` (the legacy float still converts
by D1). When the label names a plan this service holds, any amount but its
`total_stroops` is 409 `authorization_amount_mismatch`, before anything is
built. An unheld label or an unreadable plan store is left to `/execute`.

### D5 — Settle per delivered step, return the rest, keep the proof

Each delivered step whose agent has a confirmed on-chain owner is paid exactly
its `price_stroops`; every other step's price, and any authorization above the
plan's total (the surplus), is returned by the same settle. The record keeps
`planned_stroops` per step (JSONB) and `authorized_stroops` (new nullable
`NUMERIC(39,0)` column, `ADD COLUMN IF NOT EXISTS`; old rows read null). The
task's `spent_stroops` is what the settle moved (paid runs) or the delivered
prices (simulated runs).

### D6 — Receipts reconcile

`SettlementView` gains `asset` and `totals {authorized, planned, charged,
returned, surplus}`; each step gains `planned`, `charged`, `returned`, each
`{stroops, display}`. `charged + returned = authorized` and
`Σ step.returned + surplus = returned`, exactly — pinned by a property test
over 120 random plans (1–6 steps, random prices including 7-dp ones, failures,
execute-time refusals, unusable output, unowned and unreadable owners,
over-authorization), and dispute credits by a sweep of 400 random settlements.
A v1 record shows `charged`/`returned` null per step: it moved one total.

### D7 — A credit is the share, rounded down

`credited_amount_usdc` computes `fraction_of(charged_stroops, fraction)`.

## API changes (all additive; deprecated fields kept and derived)

| Payload | New | Deprecated (derived) |
| --- | --- | --- |
| `PlanStep` | `price_stroops: int` | `est_price_usdc` |
| `DecomposeResponse` | `total_stroops: int`, `asset: {code, issuer, decimals}` | `total_usdc` |
| `POST /api/stellar/build/authorize` | `max_amount_stroops: int` (send this); 409 `authorization_amount_mismatch` | `max_amount_usdc` (now optional) |
| `Task` / `TaskSummary` | `spent_stroops: int \| null` | `spent` |
| `GET /api/tasks/{id}/disputes` → `settlement` | `asset`, `totals{authorized, planned, charged, returned, surplus}` | `settled_usdc` |
| … → `settlement.steps[]` | `planned`, `charged`, `returned` (`{stroops, display}` or null) | `price_usdc`, `paid_usdc` |
| `GET /api/stellar/network` | `asset` now the configured SAC's name | — |

## Consequences

- One `ALTER TABLE … ADD COLUMN IF NOT EXISTS` runs on the first settlement
  write after deploy. Nothing else migrates.
- Until the platform's agents are registered on-chain (P8), paid runs settle
  nothing to operators and return every stroop; the receipt says so per step.
- A console that keeps sending a float still works; one that sends a total
  other than the plan's is now refused instead of over- or under-custodied.
