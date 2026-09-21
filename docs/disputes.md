# Disputes: the window, the proof, and what an upheld dispute actually pays

This explains what recourse a buyer has after a workflow has been paid for,
what it costs the agent that was disputed, who pays for it, and how the credit
actually reaches the buyer.

Four audiences. A **buyer** who paid for a workflow and did not get what they
paid for wants "The window", "Who may dispute" and "What an upheld dispute
pays". An **operator** whose agent has been disputed wants "What can be
disputed", "What an upheld dispute pays" and "A dispute does not move
reputation" — the short version is that nothing is ever taken from you.
Whoever **runs this deployment** wants "How a dispute is adjudicated, and how
the credit is paid", plus the two operator sections at the end — the first
being the procedure for a payout that never confirmed. A **reviewer** checking
the claim against the code wants "The trust model" and "The API", and should
read `docs/decisions/0007-dispute-window.md` and
`docs/decisions/0008-refund-execution.md` alongside them.

## The window

**When a paid workflow settles, its buyer has 24 hours to dispute any step of
it.** The clock starts at settlement — the moment the charge and the
attestation land on-chain — and the exact closing time is fixed then and
recorded with the settlement.

That closing time does not move afterwards. The window length is a
configuration value (`DISPUTE_WINDOW_SECONDS`, shipped at 86 400 seconds), but
it is read **once, at settlement**, and stamped onto the settlement record;
every later check compares against the stamp. Changing the setting governs
workflows that settle after the change and nothing that already happened. A
buyer who was told a deadline keeps that deadline, and an operator whose window
has closed does not find it reopened.

A dispute raised after the window has closed is refused, and the refusal says
**when** it closed rather than only that it did. A window whose length is
discovered on rejection is not recourse.

Nothing expires the other way: the window closing does not delete the dispute
record, the settlement record, or an open dispute raised before it closed.

## Who may dispute, and how they prove it

**Only the payer.** The address that authorized the escrow and whose USDC
actually moved is recorded with the settlement, and it is the only address that
can open a dispute against that workflow.

Proof is a **signature from that wallet**, not a password, an account or a
session. The flow is the same one an operator already uses to bind an agent
endpoint:

1. Ask for a challenge. The API returns a nonce and the exact message to sign,
   along with what is being disputed — the step, what it was charged, what a
   credit would come to, and when the window closes.
2. Sign that message with the wallet that paid. The message is
   `orizon-dispute:v1:{job_id_hex}:{step_index}:{nonce}` — it names the
   protocol, the job and the step, so a captured signature cannot be replayed
   against a different step or a different workflow, and the nonce is
   single-use.
3. Post the signature with the dispute's written reason.

Both of the message encodings Stellar wallets use in practice are accepted —
the SEP-53 framing Freighter implements and the raw-bytes form — so "sign this
message" works whichever wallet the buyer holds.

**Practical consequences.** The wallet that paid has to be connected at dispute
time; remembering the address is not enough, because the signature is what
authorizes the dispute. And a buyer who no longer controls the wallet that paid
cannot dispute: there is no account to recover into, which is the same trade
permissionless payment makes everywhere else in this marketplace.

**A reason is mandatory and is kept.** It is the evidence trail — the thing a
review of the dispute actually reads — and it stays on the record whether the
dispute is upheld or rejected.

## What can be disputed

**A step that was settled.** A workflow is charged as a whole, but it is
disputed a step at a time, because a step is the unit that has a price, an
agent and an outcome.

**A step that never delivered cannot be disputed — because it was never
charged.** Only steps that produced output are billed: the charge totals the
delivered steps' prices, and a step that failed, timed out or was never
dispatched contributes nothing to it. There is no money to credit back, so
there is nothing to dispute. The settlement record keeps a `delivered` flag per
step precisely so this stays answerable a day later, when the run's trace is
long gone.

This is worth stating to buyers directly, because it reads as a refusal when it
is the opposite: a failed step already cost you nothing. What it costs the
agent is reputational — a failed step is rated like any other, and a run where
nothing was delivered is rated too.

**One dispute per step.** A second dispute against the same step of the same
job is not an error the buyer has to act on: the API answers it with **the
original dispute, unchanged**, including its status and reason. Disputing a
different step of the same workflow is a separate dispute and is allowed.

**A workflow that was never paid for cannot be disputed at all.** A simulated
run — no wallet, no authorization — charges nothing, settles nothing and has no
window.

## What an upheld dispute pays, and who pays it

**The credit is the disputed step's settled charge**, under a stated policy
rather than a case-by-case judgement: `DISPUTE_CREDITED_FRACTION` ships at
`1.0`, the whole of what that step cost. The amount is computed when the
dispute is opened and frozen on its record, so a later change to the policy
cannot rewrite what the buyer was shown, and it is clamped so a credit can
never exceed what was actually charged for the step.

What is actually transferred is the **smallest** of three numbers: the amount
frozen on the dispute when it was opened, the policy share of that step's price
under the fraction in force at adjudication time, and what the workflow's
charge actually moved on-chain. With the shipped policy those are normally the
same number. They can differ, because the per-step figure starts life as the
plan's *quoted* price while the charge's total is what was really submitted,
and when they differ the credit follows the smallest — the platform never
refunds money it did not collect.

One consequence of that middle term is worth stating rather than discovering.
Because the frozen amount is only ever an upper bound, *lowering*
`DISPUTE_CREDITED_FRACTION` does reach disputes that are already open, while
raising it cannot. The guarantee is one-directional on purpose: a buyer can
never be paid more than the figure they were shown, and a tuning change can
never retroactively increase what the platform owes on work already done.
A credit is also refused outright above `MAX_REFUND_USDC` (shipped at `1.0`),
checked before anything is signed; a step on this deployment settles for
hundredths of a USDC, so that ceiling only ever catches something that has gone
wrong.

Crediting one step is what makes this a *partial* refund: the rest of the
workflow — the steps that did deliver — stays paid, and their agents keep their
earnings.

**The credit is funded by the platform, and never clawed back from the agent.**
It is a transfer from the settler's own wallet to the buyer over the asset
contract — **not** a reversal of the original charge, and **not** a seizure of
anything the agent was paid:

- The deployed `PaymentEscrow` has no refund entrypoint and never takes
  custody — `charge` sends USDC from the payer straight to the agent's owner,
  so there is nothing held anywhere to reverse.
- Nothing in the system can take funds back out of an agent owner's wallet, and
  nothing tries to. An operator's settled earnings are final.

The full reasoning, the rejected alternatives and the testnet proof that a real
credit lands are in `docs/decisions/0002-partial-credit-refund.md`.

## How a dispute is adjudicated, and how the credit is paid

A person decides, through an authenticated route, and the payout that follows
is ordered by that decision alone. There is no automatic rule that upholds a
dispute, and nothing on-chain weighs the claim.

**Rejecting** is one write, and it is only ever made on a dispute that is still
`open`: the dispute moves to `rejected` with its resolution time, the reason it
was opened with stays on the record, and nothing is signed or spent. Any other
status is refused rather than absorbed — a dispute that is already paid, that
is mid-payout, or that has already been rejected cannot be rejected again,
because that would be a second adjudicator quietly overruling the first. The
adjudicator may attach a note; it is logged with the decision rather than
written onto the dispute, which carries the buyer's evidence and not the
platform's commentary on it.

**Upholding** is where money moves, and it happens in a fixed order:

1. The dispute is recorded `upheld`. That is the verdict, and it is durable
   before anything else is attempted.
2. A **refund claim** is taken on that dispute. It is a row in a table, so it
   survives a restart, and only one caller can ever hold it. Taking it moves
   the dispute to `crediting`. If the claim cannot be taken — because another
   payout already holds it, or because the dispute is not in a state that can
   be paid — nothing is signed at all.
3. The amount is computed and checked against `MAX_REFUND_USDC` **before** any
   transaction is built.
4. The settler signs a transfer of that amount to the buyer over the asset
   contract.
5. On success the dispute moves to `credited` with the refund transaction hash
   on its record, and the claim is dropped.

The claim is what makes a credit payable exactly once. Two adjudicators
clicking at the same moment, a retried request, a redeployed process mid-flight
— all of them meet the same row, and only one gets past it.

**A repeat uphold is therefore never a second payout.** An adjudicator who
double-clicks, or a console retrying a dropped response, is answered with the
dispute as it now stands: an already-credited dispute comes back with the same
record and the same transaction hash, and one that another caller is part-way
through paying comes back as whatever that caller has made of it. The one
repeat that is refused outright is an uphold aimed at a dispute stuck in
`crediting` — that is the reconciliation case at the end of this document, and
it is refused precisely so it cannot be retried into a double credit.

**When the transfer definitively fails** — the network rejected it, so no money
moved — the claim is released and the dispute goes back to `upheld`, payable
again. A buyer who was not paid stays payable.

**When the transfer times out, nothing is retried.** A submission that timed
out may still settle, so the dispute stays in `crediting` with the in-flight
transaction hash recorded, and an operator reconciles it against the chain.
This is a deliberate trade: **paying late rather than ever paying twice.** A
late credit is a support question; a double credit comes out of the platform's
own wallet and cannot be reversed, because the asset contract has no more of an
undo than the escrow does. If a buyer's dispute sits in `crediting`, it has not
been forgotten — it is waiting on a person with a block explorer.

## The trust model, stated plainly

- **The platform funds the credit.** The disputed agent's only consequence is
  reputational — never a seizure of its funds.
- **The platform adjudicates the dispute.** A human reviews the reason and the
  settled record and decides. There is **no on-chain arbitration** in this
  sprint: no contract weighs the claim, and no escrow releases on a verdict.
  This is a permissioned, trusted operation, and it is disclosed everywhere
  rather than implied by the word "dispute".
- **Opening a dispute proves nothing and costs the agent nothing.** It records
  a claim; it does not establish that the claim is true.

Two things follow that are worth being honest about. Adjudication is only as
good as the person doing it, and a buyer who disagrees with a rejection has no
appeal beyond asking again. And because the credit comes from the platform
wallet, the platform must hold enough of the asset for an upheld dispute to be
payable at all.

## A dispute does not move reputation

**Raising a dispute never changes an agent's score.** Between opening and
adjudication the disputed agent's reputation reads exactly as it did before:
nothing is written on-chain, and the planner's view of that agent does not
change.

This is deliberate. A dispute anyone could raise that immediately dented a
competitor's on-chain score would be a free weapon — and because the ledger is
append-only, the dent could never be taken back when the dispute was rejected.

Only an **upheld** dispute reaches the chain, as a single low rating (10 on the
ledger's 0–100 scale). Two details an operator should know about it:

- The step's original automatic rating stays where it is. The dispute adds a
  second rating rather than amending the first, because the ledger has no
  entrypoint that amends one. The disputed step therefore carries two ratings,
  and both count toward the agent's score.
- It is written under a **derived job id** — `sha256(job_id || "dispute")`,
  truncated to 16 bytes — because the ledger refuses a second rating for the
  same `(agent, job)` pair. The dispute stays linkable to the job it disputes,
  and it is what finally moves the `disputed` count and `dispute_rate_bps` on
  the agent's reputation row.

A rejected dispute writes nothing on-chain at all. It stays on the record,
with its reason, as part of the agent's history with that buyer — not as part
of its score.

## The lifecycle

A dispute has one status at a time, and it only ever moves forward. The record
is appended to, never rewritten: the reason, the amounts and the opening time
stay as they were.

| status | what it means | what it carries | written by |
| --- | --- | --- | --- |
| `open` | raised inside the window by the payer, not yet adjudicated | the reason, the step's charge, the creditable amount, the opening time | story 4.02 — the only status it ever writes |
| `upheld` | adjudicated in the buyer's favour | the on-chain dispute rating's tx, once written | adjudication (4.03); 4.04 adds the rating tx |
| `crediting` | the credit is being paid — a claim is held on this dispute | the in-flight refund tx, once one has been submitted | the refund path (4.03) |
| `credited` | the credit has landed in the buyer's wallet | the refund tx | the refund path (4.03) |
| `rejected` | adjudicated against the claim | the resolution time; nothing on-chain | adjudication (4.03) |

```text
open ──► upheld ──► crediting ──► credited   the claim stood: the buyer is
  │        ▲            │                    credited, and the agent carries
  │        └── failed ──┘                    a low on-chain rating
  │
  └────► rejected                            the claim did not stand: nothing
                                             on-chain, the record and its
                                             reason are kept
```

**`crediting` is not a verdict.** Nobody adjudicates a dispute *into* it: it is
the refund claim itself, made durable, and it exists so that a payout can be
interrupted — by a redeploy, a restart, a second click — without ever being
paid twice. A buyer who sees it should read "your credit is being paid", not
"your dispute is being reconsidered".

A dispute leaves `crediting` in one of three ways: the transfer lands and it
becomes `credited`; the transfer definitively fails, the claim is released and
it returns to `upheld` to be paid again; or the transfer times out, in which
case it **stays** in `crediting` until an operator has checked the chain. The
last of those is the reconciliation case below.

`open` is the only status story 4.02 writes. Everything past it belongs to the
stories that pay the credit and write the rating, which is why a freshly opened
dispute shows no transactions: there are none to show yet.

## The API

| Route | Who may call it | Purpose |
| --- | --- | --- |
| `POST /api/disputes/challenge` | public | mint a single-use nonce and return the exact message to sign, with the step, its charge, the creditable amount and the window's closing time |
| `POST /api/disputes` | the payer, proved by the signature | open the dispute: job, step, written reason, nonce, signature |
| `GET /api/disputes/{dispute_id}` | anyone holding the id | read one dispute back — status, reason, amounts, and the refund and rating transactions once they exist |
| `GET /api/tasks/{task_id}/disputes` | the task's own token, or an operator API key | one workflow's dispute window and every dispute raised against it; an unknown or unsettled task is a null window and an empty list, not a 404 |
| `POST /api/disputes/{dispute_id}/uphold` | an adjudicator, with `X-API-Key` | uphold the claim and pay the credit — records `upheld`, takes the refund claim, and transfers the amount to the payer |
| `POST /api/disputes/{dispute_id}/reject` | an adjudicator, with `X-API-Key` | reject the claim — records `rejected` with its resolution time; nothing is signed and nothing is spent |

The read routes take no credential because both ids are unguessable — a dispute
id is `dsp_` plus 16 random hex characters — which is the same trade the task
read token makes, and it keeps a buyer able to check their own dispute without
an account.

**The two adjudication routes are the exception to everything above, and they
fail closed.** Every other route in this service treats an unset `API_KEY` as
"the demo is open"; these two treat it as "refuse", on every network including
testnet. They also refuse while `DISPUTE_REFUNDS_ENABLED` is false, which is
the shipped default — so a deployment that has not deliberately turned the
money path on cannot be talked into adjudicating by someone who knows a dispute
id. The reason for the divergence is that `/api/stellar/server/charge` can only
spend an allowance the payer already authorised on-chain, while an upheld
dispute spends the platform's own balance on an adjudicator's say-so with
nothing on-chain to bound it. `docs/decisions/0008-refund-execution.md` D1 has
the argument in full.

Turning the money path on without a key is not a silent weakness: with
`DISPUTE_REFUNDS_ENABLED=true`, a signing key and an asset SAC configured, the
process **refuses to start** unless `API_KEY` is set, and says why.

What an adjudicator can be told, and what each answer means:

| refusal | when |
| --- | --- |
| 503 `dispute_refunds_disabled` | the switch is off — nothing on this deployment is adjudicable. A configuration state, not the caller's mistake |
| 503 `adjudication_not_configured` | the switch is on but `API_KEY` is empty. Logged at ERROR: a live refund switch with no credential behind it is a misconfiguration someone has to see |
| 401 `invalid_api_key` | the key is missing or wrong. The answer is the same either way — an adjudication route that distinguished them would be an oracle |
| 404 `unknown_dispute` | no dispute with that id |
| 409 `dispute_not_open` | a rejection aimed at a dispute that is no longer `open` |
| 409 `dispute_rejected` | an uphold aimed at a rejected dispute: it can never be credited |
| 409 `refund_in_flight` | an uphold aimed at a dispute in `crediting`. Reconcile it by hand; never retry it |
| 409 `settlement_missing` | the settlement the dispute was judged against is no longer on record, so the credit cannot be bounded by what was actually charged |
| 409 `nothing_to_credit` | the settlement has no such step, the step never delivered, or the amount prices to zero |
| 409 `refund_above_cap` | the amount exceeds `MAX_REFUND_USDC`. Nothing was signed |
| 502 `refund_failed` | the transfer definitively did not settle, so no funds moved. The dispute is back to `upheld` and can be credited again |
| 504 `refund_unconfirmed` | the transfer was submitted and its outcome is unknown. The dispute stays in `crediting` for reconciliation |

`settlement_missing`, `nothing_to_credit` and `refund_above_cap` are all raised
**before** anything is signed, and each hands the refund claim back, so the
dispute stays payable once whatever caused them is fixed. Only `refund_failed`
and `refund_unconfirmed` describe a transaction that was actually submitted,
and only the second of those leaves the claim held.

A dispute is refused, with the reason said plainly, when: the window has closed
(the response says when it closed); the signature does not verify against the
payer recorded at settlement; the nonce is missing, expired or already used;
the step never delivered and so was never charged; or there is no settlement
record for the job at all. A **duplicate** is not refused — the original
dispute comes back unchanged.

## For operators: where the records live

Settlements and disputes are the first things this backend keeps that are not
on-chain and not disposable. They live in Postgres when `DATABASE_URL` is set,
behind the same store seam the endpoint bindings use, in append-only tables.

**Without `DATABASE_URL` the service falls back to an in-memory store**, and
the whole of this document becomes true only until the next restart — which a
free-tier instance performs whenever it idles. The fallback is there so local
development and the test suite need no database; it is not a deployment. It
says so once at startup, and it logs a warning naming any record it drops, so a
window that can no longer be honoured is never silent.

One read is weaker than the records behind it. `GET /api/tasks/{task_id}/disputes`
is gated by the task's read token, and those tokens live in memory with the task
state, not in Postgres — so after a restart that listing answers as though the
task were unknown, even though the settlement and its disputes survived. Nothing
a buyer needs is lost: opening a dispute is gated by their wallet signature and
never by the task token, and `GET /api/disputes/{dispute_id}` keeps working. The
per-task view is a convenience for the console, and it is the console that holds
the token.

A settlement is recorded after the charge and the seal have landed, so it can
never fail the workflow. If it cannot be written, the workflow is paid and
attested but has **no dispute window**, and the only trace of that is the error
log — which is why that failure is logged with the same weight as an
attestation that did not settle.

## For operators: reconciling a dispute stuck in `crediting`

A dispute sitting in `crediting` has a refund claim held on it and a transfer
this service could not confirm. The submission timed out, or the call raised,
or the process was cancelled between sending and confirming — in each of those
cases the transaction may be on the network, and whether it settled is knowable
only from the chain.

**Do not simply retry the payout.** An unconfirmed transaction may still land,
and a second transfer would credit the buyer twice out of the platform's own
wallet, with nothing that can reverse it. The claim is deliberately left in
place to stop that happening, and the uphold route refuses a dispute in
`crediting` outright — so the obvious retry is already blocked. What must also
be resisted is the workaround: releasing the claim by hand, or otherwise
returning the dispute to `upheld`, before the chain has been read. That is the
one action that turns a recoverable delay into an unrecoverable loss.

**Find them.** The claim table holds exactly the disputes that are mid-payout
— it is emptied inside the same statement that credits, releases or rejects —
so anything in it whose claim is more than a few seconds old is stuck. The
store reads it back oldest-first as `list_refund_claims()`, which is the
reconciliation queue; the same thing straight from the database is:

```sql
SELECT dispute_id, to_timestamp(claimed_at) AS claimed_at
FROM refund_claims
ORDER BY claimed_at, dispute_id;
```

The error log is the other half of the picture, and it carries the four facts
worth having at the moment it happened: the dispute id, the job id, the payer
and the amount. Search it for the dispute id before touching anything.

**Check the chain, in this order.**

1. **The in-flight hash.** The dispute record carries it —
   `GET /api/disputes/{dispute_id}` returns it as the refund transaction. Look
   it up on Horizon or Stellar Expert. A transaction found and successful
   means **the buyer has been paid**: the credit landed, and only this
   service's record of it is missing. If the record carries **no hash at all**
   — which is what a submission that raised before it had one looks like —
   there is nothing to look up, so go straight to the next step.
2. **The settler's own history**, if the hash turns up nothing. Read the
   settler account's transfers over the asset contract around the claim time,
   looking for one to that payer for that amount. A timeout is exactly the case
   where this service's view and the chain's disagree, so confirm from the
   account rather than concluding from a single absent hash.
3. **Only after both** is it safe to say the transfer never landed.

**Then settle the record to match the chain**, and only then:

- **It succeeded.** The buyer has been credited. Close the dispute by recording
  what landed — `append_status(dispute_id, "credited", refund_tx=<hash>)`.
  Ordering the payout again instead would credit them a second time.
- **It failed, or the hash is on no explorer and the payer's balance never
  moved.** Nothing moved, so `release_refund_claim(dispute_id)` returns the
  dispute to `upheld`, and only then may the payout be ordered again.
- **You cannot tell.** Leave it. Late is recoverable; twice is not.

Both writes are deliberate and manual, because the decision is the part that
matters and it is one a person has to make by reading the chain. The tooling
will not make it for you: `scripts/uphold_dispute.py` refuses a dispute in
`crediting` and prints this same block with the explorer links filled in, and
so does the API, with `refund_in_flight`.

Related: `docs/decisions/0008-refund-execution.md` (why adjudication is an
authenticated route, why the claim is taken before signing and why a timeout is
never retried), `docs/decisions/0007-dispute-window.md` (why the window, the
signature and the settlement record are shaped this way),
`docs/decisions/0002-partial-credit-refund.md` (where the credit comes from and
what was rejected) and `docs/reputation.md` (what a rating is worth and how the
floor uses it).
