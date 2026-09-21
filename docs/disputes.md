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
