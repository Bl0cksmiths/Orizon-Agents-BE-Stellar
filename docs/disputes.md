# Disputes: the window, the proof, and what an upheld dispute actually pays

This explains what recourse a buyer has after a workflow has been paid for,
what it costs the agent that was disputed, and who pays for it.

Three audiences. A **buyer** who paid for a workflow and did not get what they
paid for wants "The window", "Who may dispute" and "What an upheld dispute
pays". An **operator** whose agent has been disputed wants "What can be
disputed", "What an upheld dispute pays" and "A dispute does not move
reputation" — the short version is that nothing is ever taken from you. A
**reviewer** checking the claim against the code wants "The trust model" and
"The API", and should read `docs/decisions/0007-dispute-window.md` alongside
them.

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
frozen on the dispute when it was opened, the disputed step's price on the
settlement record, and what the workflow's charge actually moved on-chain.
With the shipped policy those are normally the same number. They can differ,
because the per-step figure starts life as the plan's *quoted* price while the
charge's total is what was really submitted, and when they differ the credit
follows the smallest — the platform never refunds money it did not collect.
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

**Rejecting** is one write: the dispute moves to `rejected` with its resolution
time, the reason it was opened with stays on the record, and nothing is signed
or spent.

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
| `upheld` | adjudicated in the buyer's favour | the on-chain dispute rating's tx, once written | story 4.04 |
| `credited` | the credit has been paid to the buyer | the refund tx | story 4.03 |
| `rejected` | adjudicated against the claim | the resolution time; nothing on-chain | adjudication |

```text
open ──► upheld ──► credited      the claim stood: the buyer is credited and
  │                               the agent carries a low on-chain rating
  └────► rejected                 the claim did not stand: nothing on-chain,
                                  the record and its reason are kept
```

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

The read routes take no credential because both ids are unguessable — a dispute
id is `dsp_` plus 16 random hex characters — which is the same trade the task
read token makes, and it keeps a buyer able to check their own dispute without
an account.

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

Related: `docs/decisions/0007-dispute-window.md` (why the window, the
signature and the settlement record are shaped this way),
`docs/decisions/0002-partial-credit-refund.md` (where the credit comes from and
what was rejected) and `docs/reputation.md` (what a rating is worth and how the
floor uses it).
