# Disputes: the window, the proof, and what an upheld dispute actually pays

This explains what recourse a buyer has after a workflow has been paid for,
what it costs the agent that was disputed, and who pays for it.

Three audiences. A **buyer** who paid for a workflow and did not get what they
paid for wants "The window", "Who may dispute" and "What an upheld dispute
pays". An **operator** whose agent has been disputed wants "What can be
disputed", "Who pays" and "A dispute does not move reputation" — the short
version is that nothing is ever taken from you. A **reviewer** checking the
claim against the code wants "The trust model" and "The API", and should read
`docs/decisions/0007-dispute-window.md` alongside them.

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

Crediting one step is what makes this a *partial* refund: the rest of the
workflow — the steps that did deliver — stays paid, and their agents keep their
earnings.

**The platform pays it.** The credit is a transfer from the settler's own
wallet to the buyer over the asset contract. It is **not** a reversal of the
original charge and **not** a clawback from the agent:

- The deployed `PaymentEscrow` has no refund entrypoint and never takes
  custody — `charge` sends USDC from the payer straight to the agent's owner,
  so there is nothing held anywhere to reverse.
- Nothing in the system can take funds back out of an agent owner's wallet, and
  nothing tries to. An operator's settled earnings are final.

The full reasoning, the rejected alternatives and the testnet proof that a real
credit lands are in `docs/decisions/0002-partial-credit-refund.md`.

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
