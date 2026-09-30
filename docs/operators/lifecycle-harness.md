# The lifecycle harness (story 5.01)

`python -m scripts.lifecycle` drives the **deployed** API through the whole
buyer lifecycle on **testnet**, with one buyer wallet and one external agent
(or several: `--agent` repeats, see [AC5](#ac5--an-external-endpoint-that-stops-answering-mid-workflow)),
and writes every transaction hash into an evidence file the moment the hash
exists. Story 5.05's evidence index is built from that file.

    decompose → authorize → execute → poll → verify → dispute → uphold → refund → reputation

It is an operator tool, not a test. It signs with a real (testnet) buyer key
and adjudicates with the real operator key, so it moves real testnet funds:
the buyer's authorization, the operator's payout, and the platform-funded
refund. Every write happens **once**. It never retries a submit, an execute or
an uphold; when one of them has an unknown outcome it reads the state back,
records what the ledger says, and stops for a human.

It exercises the same API the dApp does, the same way. Each call is annotated
with the backend route and the frontend call it mirrors in
`scripts/lifecycle/api.py`, and `tests/test_lifecycle_contract.py` pins the
bodies, headers and message formats against the backend's own OpenAPI schema.

---

## Prerequisites

| # | What | How to check |
|---|------|--------------|
| P1 | The API is on **testnet**. The harness refuses anything else, checking both the API and its RPC. | `curl -s https://orizons.xyz/api/stellar/network \| jq .network_passphrase` gives `"Test SDF Network ; September 2015"` |
| P2 | **Escrow v2** is deployed and configured. v1 cannot settle an external payer's funds (D-039); against v1 the harness runs up to `verify` and stops there with exit 7, saying so. | The harness prints `escrow v2 (version() view)` in its first lines; a dry run is enough to see it |
| P3 | Two **external, bound** agents, owned by wallets that are neither buyer nor settler. | `curl -s https://orizons.xyz/api/agents \| jq '.[] \| select(.source=="onchain" and .bound==true) \| .id'` |
| P4 | Two **funded buyer wallets**, one per run. The harness refuses an account that does not exist. | `curl -s "https://friendbot.stellar.org?addr=<G...>"` |
| P5 | The **operator API key**, and `DISPUTE_REFUNDS_ENABLED=true` on the deployment for the length of the run. | `POST /api/disputes/{id}/uphold` answers 503 `dispute_refunds_disabled` otherwise; the harness stops with exit 5 |
| P6 | The backend repo's virtualenv. | `source .venv/bin/activate` from the backend repo root |

## Environment variables

The two secrets are passed **by variable name**, never on the command line,
so they stay out of shell history and `ps`. Name them what you like.

| Variable (example name) | Holds | Flag that names it |
|---|---|---|
| `BUYER_1_SECRET`, `BUYER_2_SECRET` | a buyer wallet's `S...` seed | `--buyer-secret-env` |
| `ORIZON_API_KEY` | the deployment's operator `API_KEY` | `--adjudicator-key-env` |

Nothing the harness prints or writes contains either value, the signed
envelope, the task read token, the SEP-53 signatures or the dispute read grant.
Every line goes through one redacting output path (`scripts/lifecycle/redact.py`).
Public keys and transaction hashes are printed in full because they are the
evidence.

## Command line

| Flag | Meaning |
|---|---|
| `--api URL` | The deployed base, for example `https://orizons.xyz`. `/api` is added, and a trailing `/api` is stripped, as the frontend does. |
| `--agent ID` | The external agent this run is about. The plan must route to it, or the run stops before anything is signed (exit 4). **Repeatable:** give it once per agent for a multi-agent plan, which must route to every one of them, in any order, or the run stops with exit 4. The first `--agent` names the run, is the one whose reputation is snapshotted, and is disputed when its step delivered; otherwise the next one whose step did. With one `--agent` the harness behaves exactly as it always has. |
| `--intent TEXT` | What the buyer asks for. Required for a fresh run. Word it towards the agent's skills. |
| `--buyer-secret-env NAME` | See above. A value shaped like a seed is refused, and not echoed. |
| `--adjudicator-key-env NAME` | See above. Needed only to run through `uphold`, and not for `--dry-run`, which signs nothing. A dry run without it says the real run will need it. |
| `--evidence-dir DIR` | **One directory per run.** Holds `lifecycle.jsonl`, `lifecycle.md` and `state.json`. |
| `--dry-run` | Does every read (warm-up, network, RPC, agents, reputation, escrow version, buyer balance), then prints the plan. Builds, signs and writes nothing. It exits 0 only when the real run with the same flags would not stop on something the reads already show. Otherwise it lists every such blocker and exits 3: a buyer that does not exist on testnet (a fresh run), an agent that is not external and bound (a fresh run), or a v1 escrow when the run would reach `verify`, where a real run stops with exit 7 after the buyer has signed. |
| `--until STAGE` | Stop after this stage. |
| `--from-task TASK_ID` | Resume at `poll` for this task, for example after a backend restart. |
| `--from-dispute DISPUTE_ID` | Resume at `uphold` for this dispute. |
| `--dispute-reason TEXT` | The buyer's reason on the dispute (a harness sentence by default). |
| `--rpc-url URL`, `--horizon-url URL` | Override the RPC the API names, or Horizon (testnet's by default). |

## What each stage does

| Stage | Calls (as the dApp makes them) | Evidence rows |
|---|---|---|
| preflight | `GET /api/health` until it answers (the Render wake-up, as `components/backend-warmup.tsx` does it), `GET /api/stellar/network`, RPC `getNetwork`, `GET /api/agents` (every `--agent` must be listed, external and bound), `GET /readiness` (best effort), escrow `version()` | `preflight`, `reputation_snapshot` (`start`) |
| decompose | `POST /api/orchestrator/decompose {intent}`, refused (exit 4) unless the plan routes to every `--agent`; then `GET /api/stellar/agent/{id}` per routed agent and SAC `balance` for the buyer and each owner | `plan` (or `plan_missing_agent`), `balances_before` |
| authorize | `POST /api/stellar/build/authorize {payer, agent_id, max_amount_usdc, ttl_seconds}`; the envelope is decoded and checked to be `PaymentEscrow.authorize` from the buyer for that cap before it is signed; `POST /api/stellar/submit {signed_xdr}` once | `authorize` (tx) |
| execute | `POST /api/orchestrator/execute {plan_id, auth_id_hex, payer}` once, with the auth id read off the submit's `return_value` exactly as `execution-plan.tsx` reads it | `execute` |
| poll | `GET /api/tasks/{id}` and `GET /api/trace/{id}` with `X-Task-Token` until the task is terminal; a reputation snapshot each time the trace says the agent was rated; each rating's full hash recovered from the ReputationLedger's `rated` events | `reputation_snapshot` (`after_rating_N`), `task_terminal`, `rating` (tx) per rating, `reputation_snapshot` (`after_ratings`) |
| verify | `GET /api/tasks/{id}/disputes`; RPC `getTransaction` and `getEvents` on the settle transaction; the escrow's `authorization` view; SAC balances again; `AttestationRegistry.get(job_id)` | `settle` (v2) or `charge` (v1) (tx), `balances_after`, `seal` (tx), `settlement_checks` |
| dispute | `POST /api/disputes/challenge`, a SEP-53 signature over the returned message verbatim, `POST /api/disputes`; then the D-067 read grant (`POST /api/disputes/read-challenge`, sign, `POST /api/disputes/read-grant`) | `dispute_opened` |
| uphold | `POST /api/disputes/{id}/uphold` with `X-API-Key`, once, and only if the dispute is `open` | `upheld` |
| refund | `GET /api/disputes/{id}` with `X-Dispute-Read-Grant` until `credited` with a confirmed `rating_tx` | `refund` (tx), `dispute_rating` (tx), `reputation_snapshot` (`after_dispute`) |
| reputation | `GET /api/stellar/reputation`, the read the agents page makes | `reputation_snapshot` (`final`), `reputation_summary` |

**The authorization's label and TTL follow the escrow.** On v1 the harness
sends what the console on `main` sends: `agent_id: "orizon_batch"`,
`ttl_seconds: 600`. On v2 it follows the frozen interface
(`docs/escrow-v2-interface.md` in the contracts repo, amended to bind the
label to the plan id): `agent_id` is the plan id, and `ttl_seconds` is 1800,
the frontend's v2 value, so a worst-case run can still settle before expiry.

**What `verify` checks on v2:** one `charged` event per delivered step whose
agent has an on-chain owner, its topic naming that step's agent and its amount
the step's payout (the receipt's `paid_usdc` when the backend states it); the
charged sum equal to the settlement's `settled_usdc`; one `settled` event
whose `spent` is that sum and whose `returned` is the cap minus it; the
authorization view reading `settled` and `spent`; the buyer's balance down by
exactly the paid sum plus the authorize fee; each operator's balance up by
exactly what their agents were paid (reported as "not isolatable" when the
operator is also the buyer or the settler); the seal on the registry naming
the agent, carrying the settled total and the charged receipts. **On v1:** the
charge transaction's status and its `charged` event.

**With more than one `--agent`, `verify` adds the partial-delivery checks**
(AC5), each read from the chain:

| Check | Passes when |
|---|---|
| `seal_names_every_agent` | `AttestationRegistry.get(job_id).agents` lists every `--agent`. The seal records the whole plan in plan order, the agent that failed included; what was *paid* is in its receipts. |
| `seal_receipts_are_delivered_steps` | The seal's receipts are exactly the settlement's delivered steps' `receipt_id_hex`, and no undelivered step carries one. With `seal_receipts_match_charges` (sealed = the settle's `charged` receipts) this ties seal, settle and settlement together. |
| `failed_step_rated_20:<agent>` | Each step that did not deliver cost its agent a landed 20/100: counted per agent, from the ReputationLedger's `rated` events the poll stage read back, never from the trace. `failed_steps_rated_20` when every step delivered. "Not measured" when the ratings could not be read (the task was gone from the backend, or `getEvents` refused). |

The rest of AC5 is already in the v2 checks above: `v2_charged_per_delivered_step`
(the settle's `charged` events cover exactly the delivered, paid steps),
`v2_settled_event` (`returned` equals the authorized max minus the paid sum)
and `v2_buyer_balance_delta` (the buyer is down the paid sum plus the
authorize fee, nothing more).

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | Done through `--until`. | Nothing. |
| 3 | Refused before anything was signed: wrong network, missing variable, unfunded buyer, agent not external and bound, backend never woke. From `--dry-run`, also: a blocker the real run would hit, each one named (a buyer that does not exist, an agent that is not external and bound, or a v1 escrow on a run that reaches `verify`). | Fix the named precondition, then dry-run again until it exits 0. |
| 4 | The plan does not route to every `--agent`. Nothing signed. The `plan_missing_agent` row names the plan and, with several agents, which are missing. | Reword `--intent`, in a new `--evidence-dir`. |
| 5 | A definitive failure: the ledger or the server said no. | Read the last rows. |
| 6 | **Unknown outcome.** A submit, execute, uphold or dispute open was sent and its answer lost. The ledger has been read back and the result recorded. | Look the hash up on Stellar Expert before anything else. Never rerun the stage blindly. |
| 7 | The chain does not show what the API says happened (or, on v1, there is no settlement). | This is a finding. Record it. |
| 8 | Waited the whole budget for a task or a credit. | Resume with `--from-task` / `--from-dispute` once it moves. |
| 9 | The evidence directory already holds a run (or an unconfirmed authorize). | Resume it, or use a new `--evidence-dir`. A fresh run there could authorize a second payment. |

---

## The two-run recipe (AC1–AC3, AC6)

Dry-run each first. It prints the escrow version, the agent's record and the
buyer's balance, and nothing is built or signed. Go on to the real run only
when the dry run exits 0; exit 3 names what would stop the real run. A dry
run does not need `ORIZON_API_KEY`.

```bash
source .venv/bin/activate
export BUYER_1_SECRET=S...   # buyer wallet 1
export BUYER_2_SECRET=S...   # buyer wallet 2, a different account
export ORIZON_API_KEY=...    # the deployment's operator key

python -m scripts.lifecycle --api https://orizons.xyz --agent <agent_1> \
  --intent "<a task agent 1's skills fit>" \
  --buyer-secret-env BUYER_1_SECRET --adjudicator-key-env ORIZON_API_KEY \
  --evidence-dir docs/evidence/5.01/run-1 --dry-run
```

Then the same command without `--dry-run`. Then run 2, with the other agent,
the other buyer, and its own directory:

```bash
python -m scripts.lifecycle --api https://orizons.xyz --agent <agent_2> \
  --intent "<a task agent 2's skills fit>" \
  --buyer-secret-env BUYER_2_SECRET --adjudicator-key-env ORIZON_API_KEY \
  --evidence-dir docs/evidence/5.01/run-2
```

Nothing in the harness is specific to either agent or buyer (AC3): the two runs
differ only in their flags.

For AC6 (reputation observably different at each stage, `source` moving from
`prior` to `onchain`), start from an agent that has never been rated. Its
`start` snapshot reads `source: prior`, each `after_rating_N` shows the score
move, and `after_dispute` shows it fall. To give it "several workflows" before
the dispute, run `--until verify` a few times, each in its own directory, then
the full run. The `reputation_summary` row states whether each stage moved.

## AC4 — a backend restart between seal and dispute

```bash
python -m scripts.lifecycle ... --evidence-dir docs/evidence/5.01/ac4 --until verify
# note the task id it prints, then restart the backend:
#   Render dashboard → the service → Manual Deploy → Restart service
# wait for https://orizons.xyz/api/health to answer again, then:
python -m scripts.lifecycle ... --evidence-dir docs/evidence/5.01/ac4 --from-task <task_id>
```

The restart forgets the task and its read token (both live in memory). The
harness records `task_not_in_memory`, reads the settlement from the dispute
store, which survives the restart, or from its own state file when
`TASK_AUTH_REQUIRED` gates the listing, and disputes. The dispute being
accepted and credited after the restart is the AC4 evidence. Leave out
`--intent` on the resume. `--from-task` does not need it, and nothing is
decomposed or authorized again.

## AC5 — an external endpoint that stops answering mid-workflow

> Given an external agent that stops responding partway through a workflow,
> when the workflow completes, then the buyer is charged only for delivered
> steps and the workflow still seals.

One run, one plan, two external agents: a healthy one that delivers and a
faulty one that never answers. The harness names both, refuses a plan that
leaves either out, and proves from the chain which one was paid.

**1. The faulty agent.** A second copy of the reference agent
(`orizon-agents-Example-Agent-Stellar`, README "Fault injection" and "A second
agent that stops answering"), registered and bound from its own wallet, with
skills distinct from the healthy agent's, deployed with:

| Variable | Value |
|---|---|
| `FAULT_MODE` | `hang_after:0`: every dispatch is held open past the orchestrator's 100 s deadline and never answered, so its step fails as `response_timeout`, unbilled |
| `ORIZON_SIGNER` | the deployment's `dispatch_signer` (from `GET /api/stellar/network`), pinned; the agent refuses to start a fault mode without it |
| `ORIZON_NETWORK` | `testnet`; the agent refuses a fault mode on any other network |
| `ORIZON_ENDPOINT_URL` | its own URL, exactly as bound |

Check it before spending a workflow on it:

```bash
curl -sS https://<faulty-agent-host>/ | jq .fault_injection   # "hang_after:0 scope=process"
curl -sS https://<healthy-agent-host>/ | jq .fault_injection  # null
```

**2. Dry-run, then run.** Name the healthy agent first (it is the one
snapshotted and disputed) and the faulty one second, with an intent that needs
both agents' skills:

```bash
source .venv/bin/activate
export BUYER_1_SECRET=S...   # a funded testnet buyer
export ORIZON_API_KEY=...    # the deployment's operator key

python -m scripts.lifecycle --api https://orizons.xyz \
  --agent <healthy_agent_id> --agent <faulty_agent_id> \
  --intent "<one task that needs both agents' skills>" \
  --buyer-secret-env BUYER_1_SECRET --adjudicator-key-env ORIZON_API_KEY \
  --evidence-dir docs/evidence/5.01/ac5 --dry-run
```

Then the same command without `--dry-run`. Exit 4 means the planner left one
of them out, and nothing was signed: reword the intent and use a new
`--evidence-dir`. Expect the faulty step to take about 100 s; the task budget
is 900 s.

**3. What proves AC5.** In the `settlement_checks` row, all of these PASS:

- `v2_charged_per_delivered_step`: `charged` events for the healthy agent's
  step and none for the faulty one's;
- `v2_settled_event`: `returned` is the authorized max minus the paid sum, so
  the faulty step's price went back to the buyer;
- `v2_buyer_balance_delta`: the buyer is down exactly the paid sum plus the
  authorize fee;
- `seal_tx`, `seal_on_registry`, `seal_receipts_match_charges`,
  `seal_receipts_are_delivered_steps`: the workflow sealed, carrying only the
  delivered step's receipt;
- `failed_step_rated_20:<faulty_agent_id>`: the faulty agent took a landed
  20/100.

The `settlement_checks` row's `steps` shows the faulty step `delivered: false`,
`paid_usdc: 0.0`, `receipt_id_hex: null`. The task itself reads `complete`
when a delivered step shipped an artifact (the reference agent always does),
and `failed` when none did; either way the delivered work is charged, sealed
and disputable. The dispute stage then disputes the healthy agent's step.

A plan may also route to a seeded `agt_*` agent. Its step delivers but is not
paid (no on-chain owner), so it has no `charged` event and no receipt; the
checks expect exactly that.

Each run costs the faulty agent a 20/100, so after a few it falls below the
reputation floor and stops being planned: register a fresh agent id rather
than fighting it. Unset `FAULT_MODE` and redeploy (or delete the faulty
service) when you are done.

**Variants.** `hang_after:1` with `FAULT_SCOPE=intent` serves the faulty
agent's first step of a workflow and hangs on its second, for a plan that
hires it twice. For a control run, point the same two `--agent` flags at two
healthy agents: `failed_steps_rated_20` then reads "every step delivered".

## Feeding story 5.05

Each run directory holds:

- `lifecycle.jsonl`, the evidence. Append-only, one JSON object per line,
  fsynced as it is written, so a crash keeps every row before it. A resumed
  run appends to the same file under the same `run_id`. Each row has `seq`,
  `run_id`, `stage`, `event`, `utc`, `network`, `tx_hash`, `explorer`
  (`https://stellar.expert/explorer/testnet/tx/<hash>`), `contract`, `agent`,
  `buyer`, `amount`, `asset` and `onchain_status`. The status is what RPC
  `getTransaction` (or Horizon, for history the RPC has dropped) answered,
  never what the harness expected. `detail` carries the rest (checks,
  snapshots, the ledger number, where the status was read from).
- `lifecycle.md`, rendered from the JSONL after every row: a transactions
  table, a reputation-snapshot table and every row in order. Paste it, or link
  it, from the 5.05 index.
- `state.json`, the run's working memory, which resumes use. It holds the
  task read token, so it is written `0600`, and the harness drops a
  `.gitignore` beside it naming it. **Do not commit or publish it.** It is
  not evidence.

Amounts are labelled with the asset the network reports. On testnet the
escrow's SAC wraps native XLM, so they read `XLM (native)`, never USDC.

### Into the evidence index

Story 5.01's rule is that every hash goes into the frontend's evidence index
(`content/evidence/index.json`) as it is produced. The evidence tool writes
the run's hashes in the index's own link shape, re-verified, so nothing is
retyped:

```sh
python -m scripts.demo_evidence docs/evidence/5.01/run-1 \
    --out-dir docs/evidence/5.01/run-1/evidence \
    --index-links docs/evidence/5.01/run-1/evidence/index-links.json
```

Browser hashes join the same file with `--rows` or `--tx`
(`docs/operators/demo-recording.md` §2). Only a hash that re-verifies SUCCESS
on testnet is ever a link:

```json
{"schema": "orizon.evidence-index-links/1", "network": "testnet", "generated_at": 1790000000,
 "items": [{"id": "6.1-D4-d",
            "links": [{"label": "Settlement for agent alpha of 0.0100000 XLM on Stellar testnet — 2026-09-24",
                       "url": "https://stellar.expert/explorer/testnet/tx/<hash>",
                       "kind": "tx", "tx_hash": "<64 hex>", "date": "2026-09-24"}]}]}
```

- Each link has exactly the index's keys, in its order: `label`, `url`,
  `kind` (always `"tx"`), `tx_hash`, `date`.
- `date` is the UTC calendar date of the ledger's `created_at` on Horizon, as
  the index's snapshot method dates every transaction. A verified hash whose
  date cannot be read gets no link, and the run exits 8: rerun.
- `label` is plain language by the index validator's rule: at least two real
  words, never a bare hash or address; a full hash or address inside one is
  shortened (`GBI2I…ADBH`). It is the `--tx`/`--rows` label when given,
  otherwise built from the kind, agent and amount, and it ends with the date.
- Items appear in the index's order, and only when they gained a link:

| `kind` (harness `event`) | Index item | What the item owes |
|---|---|---|
| `register` | `6.1-D1-c` | an agent registered from its operator's own wallet |
| `rating` | `6.1-D2-a` | the on-chain ratings the plan card shows |
| `dispute_rating` | `6.1-D3-a` | the dispute's negative on-chain rating |
| `refund` | `6.1-D3-b` | the matching partial-credit refund |
| `authorize` (`authorize`, `authorize_unknown`), `settle` (`settle`, `charge`), `seal` | `6.1-D4-d` | settlements, each with its receipt and attestation |
| `other` | `6.1-RD-f` | activity viewable on Stellar Expert |

**Merging it (the coordinator).** In the frontend repo, for each entry of
`items`, find the item with that `id` in `content/evidence/index.json` and
append its `links` to that item's `links`, skipping a link whose `tx_hash` the
item already lists. Then, by hand:

- make each label say whose wallet signed, as the index's labels do (for
  example "… by the team's QA operator key GBWMD…7BQJ (team wallet: not
  external)"). The tool cannot tell a team wallet from an outside one;
- copy a registration by an **outside** operator into `6.1-D4-c` as well;
- update the item's `status` and `note` if the new links change them, and the
  snapshot's `as_of`;
- run `npm run evidence:check` (the index's rules, offline) and
  `npm run evidence:verify` (every link re-read on the network) before
  committing.
