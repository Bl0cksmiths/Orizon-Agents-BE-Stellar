# Disputes: the window, the proof, and what an upheld dispute actually pays

This explains what recourse a buyer has after a workflow has been paid for,
what it costs the agent that was disputed, who pays for it, and how the credit
actually reaches the buyer.

Four audiences. A **buyer** who paid for a workflow and did not get what they
paid for wants "The window", "Who may dispute" and "What an upheld dispute
pays". An **operator** whose agent has been disputed wants "What can be
disputed", "What an upheld dispute pays" and "What a dispute does to
reputation" — the short version is that nothing is ever taken from you, and
that an upheld dispute costs your agent one low rating on-chain.
Whoever **runs this deployment** wants "How a dispute is adjudicated, and how
the credit is paid" and "After `credited`: the dispute rating", plus the
operator sections at the end — the procedure for a payout that never
confirmed, and the one for a dispute rating that collided. A **reviewer**
checking the claim against the code or the chain wants "The trust model",
"Reading the two on-chain artifacts" and "The API", and should read
`docs/decisions/0007-dispute-window.md`,
`docs/decisions/0008-refund-execution.md` and
`docs/decisions/0009-dispute-rating.md` alongside them.

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

1. Ask for a challenge on the job and the step. The API returns a nonce, the
   exact message to sign and when the challenge expires. What is being
   disputed — the job id to ask about, what the step was charged, what a
   credit would come to, and when the window closes — is on the task's dispute
   view, `GET /api/tasks/{task_id}/disputes`.
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
dispute is upheld or rejected. It may be up to 500 characters, a paragraph; a
longer one is refused before anything is recorded, rather than accepted and
kept only in part.

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
because that would be a second adjudicator quietly overruling the first.

**A rejection must say why, and the buyer is shown it.** The adjudicator's note
is required, up to 500 characters (a longer one is refused, not cut), and it
comes back on the dispute as `rejection_reason` — because a rejection with no
explanation is worse than no dispute system at all. A rejection without one is
refused before anything is read: no body, no `note`, a null note or an empty
one. So is a note that is nothing but whitespace or control characters once it
is cleaned (`rejection_reason_required`). Write it for the buyer: it is the only
word they get, and it can be read wherever the dispute can — see "Who can read
what a dispute says" below. It is never written to the log.

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
6. Only then — the credit landed **and** recorded — the settler writes the
   dispute rating against the agent, and its transaction hash is added to the
   same record. "After `credited`" below is what that step does, and why
   nothing it does can reach back into the five before it.

The claim is what makes a credit payable exactly once. Two adjudicators
clicking at the same moment, a retried request, a redeployed process mid-flight
— all of them meet the same row, and only one gets past it.

**A repeat uphold is therefore never a second payout.** An adjudicator who
double-clicks, or a console retrying a dropped response, is answered with the
dispute as it now stands: an already-credited dispute comes back with the same
refund transaction hash and no transfer signed, and one that another caller is
part-way through paying comes back as whatever that caller has made of it. The
one thing a repeat uphold of a `credited` dispute does attempt again is its
**rating** — never the transfer — and that is safe for the reason given in the
next section. The one
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

## After `credited`: the dispute rating

An upheld, paid dispute has one more consequence, and it is the one that falls
on the agent: the settler writes a rating against it on the ReputationLedger,
`kind = "dispute"`, scored **10 out of 100**. It is written only **once the
credit has landed and been recorded** — so no agent is ever rated for a dispute
whose buyer was not paid — and it lands as a second, separate rating beside the
one the settler wrote for that step at settlement. What it does to the agent's
score is in `docs/reputation.md`.

**It never touches the refund.** Whatever happens to the rating, the dispute
stays `credited`, its refund transaction stays on the record, and the buyer
keeps the credit. The uphold is answered with the dispute as it stands even
when the rating did not land, because by then the buyer has been paid and an
error would say otherwise.

**The record says whether it landed.** `rating_tx` on the dispute is the
answer:

- **Set** — the rating was submitted under the dispute's derived id and landed,
  or, if its submission was unconfirmed, is in flight and may still land. Look
  the hash up, or simply uphold again, which settles it either way.
- **Empty** — the buyer is paid and the agent's rating is **not known** to be
  on-chain. The dispute is not fully resolved. Usually the fix is to uphold it
  again; the log line for the attempt says when it is not, and the table below
  says what to do instead.

**Retrying is always safe, and it retries the rating alone.** Upholding a
`credited` dispute again signs no transfer — that branch returns before the
refund claim is ever looked at — and submits the rating once more. The ledger
refuses a second rating under the same id, so a retry cannot rate the agent
twice: it lands, because no earlier attempt did, or it is refused as a replay,
because one did, and the refusal costs nothing on-chain because it happens at
simulation. This is the exact opposite of the refund, where a retry after an
unconfirmed transfer is the one thing never allowed. The difference is that the
ledger can refuse a duplicate and the asset contract cannot;
`docs/decisions/0009-dispute-rating.md` D4 has the argument.

"Uphold again" means either door onto `uphold`: the route,
`POST /api/disputes/{dispute_id}/uphold`, or a re-run of
`scripts/uphold_dispute.py --dispute-id <id>`. On a `credited` dispute the
script says, before it runs anything, that the credit will not be paid again,
and then runs the rating alone; `--dry-run` previews it. Its exit code says
what became of the rating — `0` rated, or confirmed by the ledger refusing a
second copy; `12` not landed, where re-running is safe and is the fix; `13` a
collision, which no re-run can fix. It refuses a `credited` dispute with no
refund hash on record, because that one needs reconciling before anything else
is written against it.

Every attempt is logged on one line carrying every id needed to find the rating
on-chain or prove it absent —
`dispute rating <outcome>: dispute=… job=… derived=… agent=… payer=… tx=…` —
and the outcome is one of these:

| outcome | level | the record afterwards | what to do |
| --- | --- | --- | --- |
| `landed (10/100)` | INFO | `rating_tx` is its hash; the agent's cached score is dropped at once | nothing |
| `already on-chain — kept` | INFO | unchanged: an earlier attempt of this dispute landed | nothing |
| `unconfirmed — it may still land` | ERROR | `rating_tx` is the in-flight hash, when the submission returned one | uphold again. With no hash (`tx=-`), a retry that finds the rating landed after all is reported as a collision — expected, and the collision procedure resolves it |
| `failed (FAILED) — nothing landed` | ERROR | unchanged | uphold again — after fixing the cause, if the line before it names one |
| `was SUCCESS but could not be recorded on the dispute` (or `was TIMEOUT …`) | ERROR | unchanged — the store write failed, and the line carries the hash | record that hash by hand, **before** any retry: a retry first is refused as a replay with nothing on record, and reads as a collision |
| `COLLISION` | ERROR | unchanged, and no `rating_tx` | the collision procedure at the end of this document |
| `not submitted — <setting> …` | ERROR | unchanged; nothing was sent | set the setting the line names, then uphold again |
| `could not be formed` | ERROR | unchanged; nothing was sent | a retry will not mend it: the settlement no longer has the disputed step, or the job id does not derive. The dispute's records changed under it, and a person has to look |
| `not re-attempted` | ERROR, or WARNING when a `rating_tx` is already on record | unchanged; nothing was sent | the settlement that weights the rating is gone; see below |

A `failed` rating whose preceding line says `Unauthorized` is not about the
dispute at all: the deployment's signer is not the ledger's Scorer, so no
rating it signs can land. `GET /readiness` reports the same thing as
`ratings.writer = not_scorer`, and the fix — the ledger admin calling
`set_scorer` — is in `docs/reputation.md`. Uphold again once it is done.

`not re-attempted` means a repeat uphold found no settlement record for the
job, so there is no step price to weight the rating with and nothing was
submitted. It cannot happen on the first attempt, which is made with the
settlement the credit was just priced from. With `DATABASE_URL` set,
settlements are never dropped, so it is the in-memory fallback's failure.

When the workflow is still in memory, a landed rating also appears on its
trace, as `reputation → agent <id> rated 10/100 for upheld dispute <id> on step
<n> · dispute job <derived id> · tx <hash>`. That line is a convenience for a
console that still has the run on screen; the dispute record is what holds the
fact.

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

## What a dispute does to reputation

**Raising a dispute never changes an agent's score.** Between opening and
adjudication the disputed agent's reputation reads exactly as it did before:
nothing is written on-chain, and the planner's view of that agent does not
change.

This is deliberate. A dispute anyone could raise that immediately dented a
competitor's on-chain score would be a free weapon — and because the ledger is
append-only, the dent could never be taken back when the dispute was rejected.

Only an **upheld** dispute reaches the chain, as a single low rating (10 on the
ledger's 0–100 scale), and only once the buyer's credit has landed. Three
details an operator should know about it:

- The step's original automatic rating stays where it is. The dispute adds a
  second rating rather than amending the first, because the ledger has no
  entrypoint that amends one. Both count toward the agent's score, at the same
  weight — the step's quoted price, which is what every rating is weighted by.
- It is written under a **derived job id**, because the ledger refuses a second
  rating for the same `(agent, job)` pair. The derived id is the job's own first
  8 bytes followed by 8 bytes of
  `sha256(job_id ‖ "orizon-dispute:v1" ‖ step)`, so it is different for every
  disputed step of a job, and the job it disputes is readable in its first
  sixteen hex characters. "Reading the two on-chain artifacts" below shows how.
- It is what moves the `disputed` count and `dispute_rate_bps` on the agent's
  reputation row, and the next plan decomposed after it lands is routed on the
  new score rather than a cached one.

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
| `upheld` | adjudicated in the buyer's favour | nothing on-chain yet | adjudication (4.03) |
| `crediting` | the credit is being paid — a claim is held on this dispute | the in-flight refund tx, once one has been submitted | the refund path (4.03) |
| `credited` | the credit has landed in the buyer's wallet | the refund tx, and the dispute rating's tx once it is written | the refund path (4.03); the rating (4.04) adds its tx to the same status |
| `rejected` | adjudicated against the claim | the resolution time and the adjudicator's reason, which the buyer is shown; nothing on-chain | adjudication (4.03); the reason is required and buyer-facing since 4.06 |

```text
open ──► upheld ──► crediting ──► credited   the claim stood: the buyer is
  │        ▲            │                    credited, and the agent carries
  │        └── failed ──┘                    a low on-chain rating
  │
  └────► rejected                            the claim did not stand: nothing
                                             on-chain, the record and its
                                             reason are kept
```

**The rating does not get a status of its own.** It is written after the
dispute is `credited` and recorded on it as `rating_tx`, so a `credited`
dispute is paid either way, and fully resolved only once `rating_tx` is set.
One without it is a buyer who has their credit and an agent whose rating has
not landed — retryable, as "After `credited`" describes.

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

## Reading the two on-chain artifacts

An upheld dispute leaves two transactions on-chain, and both hashes are on the
dispute record: `GET /api/disputes/{dispute_id}` returns them as `refund_tx`
and `rating_tx`, beside the `job_id_hex`, `step_index`, `agent_id` and `payer`
they belong to. They are different kinds of evidence, and they tie back to the
disputed job in different ways.

**The refund transfer** (`refund_tx`) is a `transfer` on the asset contract,
signed by the settler: from the settler, to the payer, for the credited
amount. It is what shows the buyer was paid, and it carries **no job id** — a
token transfer names a sender, a recipient and an amount, and nothing else. Its
link to the job runs through the dispute record. On-chain it is corroborated
rather than proved: the recipient is the payer recorded at settlement — the
address that authorized the escrow, and the one the attestation seal names —
and the amount is no more than that step's charge.

**The dispute rating** (`rating_tx`) is a call to `ReputationLedger.submit`,
signed by the settler as the ledger's Scorer:

| argument | value |
| --- | --- |
| `agent_id` | the disputed agent |
| `job_id` | the **derived** id for the disputed step — see below |
| `rating_0_to_100` | `10` |
| `weight` | the step's quoted price, in stroops |
| `payer` | the dispute's payer |
| `kind` | `dispute` — every rating the settler writes at settlement is `auto` |

and the ledger emits a `rated` event for the agent carrying the same rating,
weight, job id and kind.

**Tying the rating to its job, without reading any code.** The derived job
id's **first sixteen hex characters are the disputed job's own**. The job's
full id is an argument of the workflow's charge (`PaymentEscrow.charge`), of its
attestation seal (`AttestationRegistry.seal`, the settlement's `proof_tx`) and
of each automatic rating the settler wrote for it. So:

1. Open the dispute rating on Stellar Expert and read its `job_id` argument.
2. Open the workflow's seal — or its charge, for the rare job whose seal did not
   land — and read the job id there.
3. The first sixteen hex characters are the same. That is the link: an
   unrelated random job id shares its first eight bytes with probability 2⁻⁶⁴.

The attestation itself reads back by the job's **full** id at
`GET /api/stellar/attestation/{job_id_hex}` — never by the derived one, which
names no attestation.

To confirm the other half as well — that the rating is for **this step** of the
job, not merely for the job — recompute it from the job id and the step index:

```bash
python3 -c 'import hashlib,sys; j=bytes.fromhex(sys.argv[1]); s=int(sys.argv[2]); print((j[:8]+hashlib.sha256(j+b"orizon-dispute:v1"+s.to_bytes(2,"big")).digest()[:8]).hex())' <job_id_hex> <step_index>
```

For job `000102030405060708090a0b0c0d0e0f`, step `0`, that prints
`00010203040506071e6388cbecdde018` — the first golden vector in
`tests/test_dispute_job_id.py`, which pins the derivation so that it can never
drift from what is already on the ledger.

An operator can see both ids before anything is paid:
`python scripts/uphold_dispute.py --dispute-id <id> --dry-run` prints the rating
the live run will write — agent, score, weight, and the job id and derived id
stacked, with their shared prefix underlined.

## The API

| Route | Who may call it | Purpose |
| --- | --- | --- |
| `POST /api/disputes/challenge` | public | mint a single-use nonce and return the exact message to sign and when the challenge expires. The step's charge, the creditable amount and the window's closing time are on the per-task read |
| `POST /api/disputes` | the payer, proved by the signature | open the dispute: job, step, written reason, nonce, signature |
| `GET /api/disputes/{dispute_id}` | anyone holding the id | read one dispute back — status, reason, amounts, the refund and rating transactions once they exist, and the receipt: what was actually credited, when it last changed, whether the rating landed and, for a rejection, why. "What a dispute returns" below has every field |
| `GET /api/tasks/{task_id}/disputes` | anyone while `TASK_AUTH_REQUIRED` is off, the shipped default; otherwise the task's own token, or an operator API key | everything a first dispute starts from, in one read: the settlement (job id, payer, each step's charge, delivery, credit and output summary, and the credit policy), the window's closing time, the server's clock, and every dispute raised on the task. An unknown or unsettled task is a null settlement, a null window and an empty list, not a 404. "What the per-task read returns" below has every field |
| `POST /api/disputes/{dispute_id}/uphold` | an adjudicator, with `X-API-Key` | uphold the claim and pay the credit — records `upheld`, takes the refund claim, transfers the amount to the payer, then writes the dispute rating. On a `credited` dispute it signs no transfer and re-attempts the rating only |
| `POST /api/disputes/{dispute_id}/reject` | an adjudicator, with `X-API-Key` | reject the claim — body `{"note": "..."}`, **required**, 1 to 500 characters, and **shown to the buyer** as the dispute's `rejection_reason`. Records `rejected` with its resolution time and that reason; nothing is signed and nothing is spent |

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
| 422 `validation_error` | a rejection with no reason — no body, no `note`, a null or an empty one — or one over 500 characters. Refused before the dispute is even read |
| 422 `rejection_reason_required` | a rejection whose note is nothing but whitespace or control characters once cleaned: there is nothing left to show the buyer. Nothing is written |
| 502 `refund_failed` | the transfer definitively did not settle, so no funds moved. The dispute is back to `upheld` and can be credited again |
| 504 `refund_unconfirmed` | the transfer was submitted and its outcome is unknown. The dispute stays in `crediting` for reconciliation |

`settlement_missing`, `nothing_to_credit` and `refund_above_cap` are all raised
**before** anything is signed, and each hands the refund claim back, so the
dispute stays payable once whatever caused them is fixed. Only `refund_failed`
and `refund_unconfirmed` describe a transaction that was actually submitted,
and only the second of those leaves the claim held.

None of these is ever about the **rating**. A rating that does not land is not
a refusal: the credit has already moved, so the uphold answers with the dispute
as it stands — `credited`, with `rating_tx` empty — and the reason is in the
log, as "After `credited`" describes.

A dispute is refused, with the reason said plainly, when: the window has closed
(the response says when it closed); the signature does not verify against the
payer recorded at settlement; the nonce is missing, expired or already used;
the step never delivered and so was never charged; or there is no settlement
record for the job at all. A **duplicate** is not refused — the original
dispute comes back unchanged.

### What a dispute returns

`GET /api/disputes/{dispute_id}`, both adjudication routes, the duplicate
answer to `POST /api/disputes` and every entry in the per-task read's
`disputes` return a dispute in this one shape. No field is ever omitted: one
that does not apply yet, or that a dispute recorded before the field existed
does not have, is null.

| field | what it is |
| --- | --- |
| `id` | the dispute's own id: `dsp_` and 16 random hex characters |
| `job_id_hex`, `step_index`, `task_id`, `agent_id` | the job the step was charged under, which step, the task it ran in and the agent that ran it |
| `payer` | the wallet that paid, and that signed the dispute |
| `reason` | the buyer's own words about what was wrong, cleaned and bounded to 500 characters |
| `status` | where the dispute stands — see "The lifecycle" |
| `charged_usdc`, `creditable_usdc` | what the step cost, and what an upheld dispute credits under the policy in force when it was opened. Both frozen at opening, so neither moves under the buyer |
| `opened_at`, `resolved_at` | when it was opened, and when it was first decided — stamped once, at the verdict. Epoch seconds |
| `refund_tx`, `rating_tx` | the refund transfer and the dispute rating, once each exists — see "Reading the two on-chain artifacts" |
| `credited_usdc` | what the refund **actually** transferred. Not `creditable_usdc`: that is the ceiling promised at opening, and the payout is bounded again when it is made — by the fraction then in force and by what the charge moved — so the two can differ, and this is the one that matches the transfer on-chain. Null until the dispute is credited, and for a dispute credited before the field existed: "not recorded", never the promise standing in for it |
| `updated_at` | when the dispute last changed state, in epoch seconds. Unlike `resolved_at` it moves: a credit reconciled hours after the verdict carries the time it was credited. Null only for a dispute last written before the field existed |
| `rating_confirmed` | whether the dispute rating is known to have **landed**. `rating_tx` cannot say on its own, because it is recorded for a submission that timed out as well as for one that succeeded. `true` once the ledger has vouched for it, `false` while it is only in flight, null when no rating was submitted or the dispute predates the field. Null means "not known", never "no" |
| `rejection_reason` | on a `rejected` dispute, the adjudicator's reason — **shown to the buyer**. Null under every other status, whatever the record holds, and for a rejection recorded before a reason was required |

**Who can read what a dispute says.** Two fields on this shape are somebody's
words rather than facts the chain already publishes: the buyer's `reason` and a
rejection's `rejection_reason`. The API does not hide either.
`GET /api/disputes/{dispute_id}` answers anyone holding the id, and the
per-task read answers anyone who may read the task — which, while
`TASK_AUTH_REQUIRED` is off (the shipped default, and how the public deployment
runs), is anyone with the task id; that read hands out every dispute id on the
task as well. The console shows both fields only to the payer, but that is a
choice about display, not about access, and it narrows nothing the API
returns. So a rejection reason is written as something anyone holding the task
id could read: about this step and this claim, with nothing about another
buyer, another dispute or the platform's internals that would not be said to
the buyer in the open. Turning `TASK_AUTH_REQUIRED` on scopes the per-task read
to the task's own token or an operator key; the single-dispute read stays a
link whose unguessable id is the credential either way.

### What the per-task read returns

`GET /api/tasks/{task_id}/disputes` is the one read a dispute receipt is built
from. A buyer's first dispute needs the job id to ask for a challenge and the
payer to know which wallet has to sign, and before story 4.05 neither was
readable anywhere; only the deadline was.

| field | what it is |
| --- | --- |
| `task_id` | the task asked about |
| `window_closes_at` | the stamped closing time, in epoch seconds; null until the task settles. Always equal to `settlement.window_closes_at`, and kept at the top level for clients that read it there |
| `now` | this server's clock when the response was built, in epoch seconds. The window is enforced by the server, so a countdown run off the browser's clock is wrong by however far that clock has drifted; the console corrects by the difference |
| `settlement` | null until the task settles; otherwise the object below |
| `disputes` | every dispute raised on the task, in the order they were opened, each in the shape `GET /api/disputes/{dispute_id}` returns |

`settlement`:

| field | what it is |
| --- | --- |
| `job_id_hex` | the job the charge was made under, which is what `POST /api/disputes/challenge` is asked about |
| `payer` | the address that authorized the escrow and whose USDC moved: the only wallet whose signature can open a dispute on this task |
| `settled_at`, `window_closes_at` | when the charge landed, and the deadline stamped from it |
| `settled_usdc` | what the charge actually moved on-chain, the ceiling on every credit |
| `charge_tx`, `proof_tx` | the charge and attestation transactions. `proof_tx` is null when the charge landed and the seal did not |
| `steps` | one entry per step of the plan, below |
| `policy` | the terms a credit is paid under, below |

Each of `steps`:

| field | what it is |
| --- | --- |
| `step_index`, `agent_id`, `agent_name` | which agent ran the step. `agent_name` may be null |
| `price_usdc` | the step's own price, which a credit is computed from |
| `delivered` | whether the step produced output. Only a delivered step was billed, and only a delivered step can be disputed |
| `creditable_usdc` | what an upheld dispute on this step would credit, computed by the server with the refund's own rule (the policy's fraction of `price_usdc`, rounded to 7 decimals), so it is the figure a dispute opened now would freeze. Exactly `0` for a step that did not deliver. The transfer is also bounded by `settled_usdc`, so this is a ceiling, never a sum the platform could exceed |
| `output_summary` | the one line the trace showed for what the step produced: the agent's own words, cleaned and bounded to 280 characters before they were stored. Null for a step that delivered nothing, and for settlements recorded before this field existed |

`policy`:

| field | what it is |
| --- | --- |
| `credited_fraction` | the share of a step's price an upheld dispute credits: `DISPUTE_CREDITED_FRACTION`, clamped to [0, 1] exactly as the refund clamps it, so it states what would really be paid |
| `funded_by` | always `platform`. The credit comes from the settler's own wallet, never from the agent |
| `adjudicated_by` | always `platform`. A person decides the claim; there is no on-chain arbitration |

**What this read exposes, and why none of it is new.** While
`TASK_AUTH_REQUIRED` is off, which is the default and how the public
deployment runs, this route is world-readable, exactly like the task and its
trace. The job id is an argument of `PaymentEscrow.charge` and a field of the
`charged` event it emits. The payer is in the `authd` event their own
authorization emitted, joined to the charge by the authorization id both
events carry. And `GET /api/tasks/{task_id}` already serves the full
`charge_tx`, so a task id already led to both on-chain. Neither is a
credential: asking for a challenge is public by design, and opening a dispute
takes the payer's signature, which knowing their address does not provide. The
output summaries are the lines the world-readable trace already showed. The
authorization id itself is left off, because no client needs it.

## For operators: where the records live

Settlements and disputes are the first things this backend keeps that are not
on-chain and not disposable. They live in Postgres when `DATABASE_URL` is set,
behind the same store seam the endpoint bindings use, in append-only tables: a
dispute's history is written, never rewritten, so its status is the newest row
rather than a field that was overwritten.

The refund claims are the one table that is not append-only, because it is not
a record — it is the mutex, one row per payout in flight, taken before anything
is signed and dropped inside the same statement that credits, releases or
rejects. It is also the reconciliation queue, which is the section below.

**Without `DATABASE_URL` the service falls back to an in-memory store**, and
the whole of this document becomes true only until the next restart — which a
free-tier instance performs whenever it idles. The fallback is there so local
development and the test suite need no database; it is not a deployment. It
says so once at startup, and it logs a warning naming any record it drops, so a
window that can no longer be honoured is never silent.

One read is weaker than the records behind it. With `TASK_AUTH_REQUIRED` on,
`GET /api/tasks/{task_id}/disputes` is gated by the task's read token, and
those tokens live in memory with the task state, not in Postgres — so after a
restart that read answers as though the task were unknown, even though the
settlement and its disputes survived. A dispute already opened is unaffected:
`GET /api/disputes/{dispute_id}` keeps working, and opening one is gated by the
payer's wallet signature, never by the task token. A **first** dispute is not
unaffected, because this read is where the console gets the job id to ask for a
challenge; after a restart under enforcement, the buyer's console cannot start
one, and an operator can read the same view with the API key. With enforcement
off, the default, none of this applies: the read is served from the durable
records and survives a restart with them.

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

## For operators: a dispute rating that collided

A collision is the one rating outcome that retrying cannot fix, and the one
that needs a person. It looks like this in the log, at ERROR:

```text
dispute rating COLLISION — the ledger already holds a rating under this dispute's derived id and this dispute records none, …: dispute=dsp_… job=… derived=… agent=… payer=… tx=-
```

and the dispute reads `credited` with `rating_tx` empty. Upholding it again
produces the same line every time. Run through `scripts/uphold_dispute.py`, it
ends in a `RATING COLLISION` block naming the agent, the dispute and the rating
id, and exits `13`.

One case is caught before it becomes a collision. When a rating lands but the
write recording it on the dispute fails, the service logs
`dispute rating was SUCCESS but could not be recorded on the dispute — record
rating_tx by hand` with the hash, and the script prints that hash with the exact
`append_status` line that records it and asks for it to be run before anything
is re-run. Do it — a re-run first is refused as a replay with nothing on
record, and reads as a collision.

**What it means.** The ledger refused the rating as a replay — it already holds
a rating for this agent under this dispute's derived id — and this dispute has
no record of ever writing one. That is one of two things, and from inside the
service they look identical:

- **Our own rating, whose hash was never recorded.** A submission that raised
  or timed out before it returned a hash, a process that died between
  submitting and recording, or a store that was down at the moment a landed
  rating was written to it. The rating is on-chain; only its receipt is
  missing. This is by far the likelier case.
- **A genuine collision** — a rating under that key that this dispute did not
  write. The derivation makes that unreachable by chance, so if it is real it
  means something other than chance: a second writer signing as the Scorer, or
  a dispute record whose job id is not the one that was settled.

The service reports both as a collision rather than guessing, because the two
wrong guesses are not equally bad: taking a real collision for our own rating
would report the dispute as resolved when its rating was never written, and
nothing would ever say so. `docs/decisions/0009-dispute-rating.md` D4 has the
argument.

**Find the rating.** Everything needed is on the log line.

1. **Search the log for the dispute id first.** A line
   `dispute rating landed (10/100)`, `dispute rating unconfirmed` or
   `dispute rating was SUCCESS but could not be recorded on the dispute` for
   the same dispute, with a `tx=` hash, names the attempt directly — a landed
   rating is logged before the record is written, precisely so this case has a
   hash to start from. Look that hash up.
2. **Otherwise, go to the chain.** Search the ReputationLedger's `rated` events
   for the agent, or the settler account's transactions from around the time
   the credit landed, for a `submit` whose `job_id` argument is the `derived=`
   value on the line.

**Decide whose it is.** It is this dispute's own rating when every argument
matches what this dispute would have written: `kind` is `dispute`, the rating
is `10`, the agent is `agent=`, the payer is `payer=`, and the weight is the
step's quoted price in stroops — the dispute's `charged_usdc` × 10 000 000,
rounded, which is the same settled step price the rating was weighted from, and
which the script's `--dry-run` prints beside the rating id.

- **It is ours.** Record it, and the dispute is fully resolved:
  `append_status(dispute_id, "credited", rating_tx=<hash>)`. The next uphold is
  then answered `already on-chain — kept`, which is the confirmation.
- **It is not ours, or nothing can be found.** Leave the dispute as it is —
  `credited`, the buyer paid, no `rating_tx` — and treat it as an incident: find
  out what else is writing ratings as the Scorer, or why the dispute's job id
  does not match its settlement. Do **not** work around it by rating the agent
  under some other id: a dispute gets exactly one reputation consequence, the
  replay guard is what enforces that, and an id minted to get past it is a
  second rating the next retry cannot see.

Never record a hash that has not been checked against the chain. A `rating_tx`
tells every later reader that the agent's consequence landed, and the uphold
will from then on treat a replay as confirmation of it.

Related: `docs/decisions/0009-dispute-rating.md` (why the rating's id keeps
the job's prefix, why it is weighted by the quoted price, why it follows the
credit, and why a rating may always be retried when a refund never may),
`docs/decisions/0008-refund-execution.md` (why adjudication is an
authenticated route, why the claim is taken before signing and why a timeout is
never retried), `docs/decisions/0007-dispute-window.md` (why the window, the
signature and the settlement record are shaped this way),
`docs/decisions/0002-partial-credit-refund.md` (where the credit comes from and
what was rejected) and `docs/reputation.md` (what a rating is worth, what a
dispute rating does to a score, and how the floor uses it).
