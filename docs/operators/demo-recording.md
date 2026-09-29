# Recording the demo video (story 5.04)

The 3–5 minute demo video shows **only real testnet transactions**, and every
one of them has to resolve on Stellar Expert. Two backend tools bracket the
recording session:

    pre-flight  →  record  →  run the harness  →  evidence sheet  →  upload  →  fill the /demo manifest

| Tool | What it answers | Reads | Writes |
|---|---|---|---|
| `python -m scripts.demo_preflight` | GO / NO-GO: is the live deployment in the state the script needs? | the deployment, the backend host's `/readiness`, Soroban RPC, Horizon, the frontend pages | nothing (one opted-in decompose aside) |
| `python -m scripts.demo_evidence` | Which transactions did the video show, and does each still verify? | the harness's `lifecycle.jsonl`, Soroban RPC, Horizon | `evidence-sheet.md`, `description.txt`, `evidence.json` |

Both are **testnet only** and refuse anything else before they judge a thing.
Neither holds or needs a secret: every flag is a public key, a URL or a number.
Run both from the backend repo root with its virtualenv active
(`source .venv/bin/activate`).

---

## 1. Pre-flight: GO / NO-GO

```sh
python -m scripts.demo_preflight \
    --buyer G...BUYER --operator G...OPERATOR --cap 0.5 \
    --operator-endpoint https://<the operator's agent> \
    --with-decompose "the intent the video types" \
    --out-dir docs/evidence/5.04/preflight
```

Run it the morning of the session, and again right before the camera rolls
(Render's free tier sleeps; the second run is also the warm-up).

| Flag | Default | Meaning |
|---|---|---|
| `--api` | `https://orizons.xyz/api` | the deployed API (a trailing `/api` is accepted) |
| `--frontend` | `https://orizons.xyz` | the deployed frontend |
| `--backend` | `https://orizon-agents-be-stellar.onrender.com` | the backend's own host, for the root-level `/readiness` that `orizons.xyz` does not proxy |
| `--buyer G...` | — | the wallet the recording pays from. **Required for GO**: without it the buyer check is SKIPPED |
| `--operator G...` | — | the wallet shown registering and binding. **Required for GO** |
| `--operator-endpoint URL` | — | the operator's reference agent, the endpoint the operator wallet binds in S03. **Required for GO**. Only the URL's origin is used and shown (a bound URL's query can carry a shared secret); `GET /` is asked there |
| `--cap` | `1.0` | the most the recorded plan may cost; the buyer must hold it plus 0.5 XLM of fees above its reserve |
| `--max-refund` | `1.0` | `MAX_REFUND_USDC` as the Render dashboard has it (no public route reports it); the settler must hold it plus 2 XLM of fees |
| `--allow-team-operator` | off | accept a buyer or operator from `app/data/team_wallets.json`; the report then says it **must be disclosed on camera** |
| `--with-decompose "<intent>"` | off | also decompose the intent and confirm the plan excludes or substitutes the below-floor agent. **Costs a model call and stores a plan** — the one write the pre-flight can make, sent once and never retried |
| `--team-register` | `app/data/team_wallets.json` | the committed register of team wallets |
| `--rpc-url`, `--horizon-url` | the public testnet endpoints | |
| `--out-dir` | — | write `demo-preflight.md` and `demo-preflight.json` |

### The checks

Every check is **PASS**, **WARN** (holds, with a caveat the take must act on),
**FAIL** (with the exact fix), or **SKIPPED** (not judged: a flag was missing
or a check it depends on failed). **SKIPPED is never a pass.** The verdict is
GO only when every required check is PASS or WARN.

| Check | Required | Source | Fails when |
|---|---|---|---|
| `network.warm` | yes | `GET /api/health`, retried for up to 120 s; the cold-start time is reported | the backend never answers |
| `network.api` | yes | `GET /api/stellar/network` | not testnet → **refused** (exit 3) |
| `build.adoption` | yes | `GET /api/ecosystem/adoption` | 404: the build predates 5.02 |
| `build.agent_readiness` | yes | `GET /api/agents/{id}/readiness` | 404: the build predates 5.02 |
| `build.readiness` | yes | `GET <backend>/readiness` | no `disputes` / `disputes.reconcile` / `escrow {contract, version}` (predates 5.01); `status` not ready; its escrow is not the one `--api` names (wrong `--backend`) |
| `escrow.version` | yes | `PaymentEscrow.version()` by simulation | the escrow is v1 — **D-039**: v1's `charge` cannot move the payer's funds. An RPC outage is a FAIL "could not be read", never "v1" |
| `escrow.settler` | yes | `settler()` vs `/readiness` `ratings.signer` | they differ (every settle reverts Unauthorized), or there is no signing key |
| `refunds.enabled` | yes | `/readiness` `disputes.reconcile.enabled`, which the backend computes as `REFUND_RECONCILE_ENABLED and DISPUTE_REFUNDS_ENABLED` | either switch is off |
| `refunds.store` | yes | `/readiness` `disputes.store` | anything but `postgres` (set `DATABASE_URL`): a free-tier sleep between the settle and the dispute loses the settlement, and 5.01 AC4 needs a durable store |
| `refunds.settler_balance` | yes | Horizon | the settler cannot spend `--max-refund` + 2 XLM above its reserve |
| `exclusion.below_floor` | yes | `GET /api/stellar/reputation` + `/params`, joined with `GET /api/agents` | no registered (`source: onchain`), routable agent (not `offline`, and `bound: true`) has a lower bound below `floor_bps` with `degraded` and `stale` both false. **A degraded or stale read never counts** — it is the prior or an old read, not a verdict — and an unbound or delisted agent never counts, since the card shows it as "no endpoint" or not at all. When **every** registered agent reads degraded (a cold start), the batch is read again, up to 3 reads 5 s apart; still all degraded is a FAIL that says no verdict could be read, never "no agent below the floor" |
| `exclusion.card_figure` | yes | the same reads, and the frontend's `(bps / 2000).toFixed(2)` (`lib/reputation-math.ts`) | the below-floor agent's bound prints as the floor's own figure (5490–5499 bps print as 2.75 against the 5500 floor's 2.75), so the row looks as if it clears. The highest bound that prints below the floor is worked out from that rule: 5489 for a 5500 floor |
| `exclusion.routable_count` | yes | the same reads | fewer than 3 routable agents (not `offline`; seeded, or on-chain with `bound: true`) clear the floor on a read that is neither degraded nor stale. The planner's starvation backstop then re-admits one below the floor, and the card reads "kept below floor", not "excluded" |
| `exclusion.decompose` | only with `--with-decompose` | `POST /api/orchestrator/decompose` | the plan's `notices` neither exclude nor substitute that agent |
| `operator.external` | yes | `GET /api/ecosystem/adoption` | no externally operated agent |
| `operator.ready` | yes | each external agent's readiness | any external agent is not `ready: true` with `reachable: done`; each is named with its failing step and the action the readiness route gives. The below-floor agent fails `routable` by design and is judged by `exclusion.below_floor` instead |
| `operator.endpoint` | yes | `GET /` on `--operator-endpoint`'s origin | no `--operator-endpoint` (SKIPPED); not a 200 with `"ok": true`; or fault injection on: a `fault_injection` field in the body or an `X-Fault-Injection` header (the reference agent sends both while `FAULT_MODE` is set) |
| `wallets.buyer` | yes | Horizon | the buyer does not exist, or cannot spend `--cap` + 0.5 XLM above its reserve |
| `wallets.operator` | yes | Horizon + `GET /api/agents` | the operator does not exist, cannot spend 0.5 XLM, or owns no registered, bound agent |
| `wallets.team` | yes | `app/data/team_wallets.json` | the buyer or operator is a team wallet (without `--allow-team-operator`; with it, WARN plus a disclosure) |
| `frontend./app/register` … `/demo` (`/app/register`, `/app/bind`, `/app/operator`, `/app/orchestrator`, `/app/agents`, `/app/trace`, `/app/ecosystem`, `/guide/list-your-agent`, `/demo`) | yes | `GET` each page, redirects **not** followed | anything but 200 (a redirect to a login is not a 200) |

A reserve is Stellar's own: (2 + subentries + sponsoring − sponsored) × 0.5 XLM,
so at least 1 XLM.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | **GO** — every required check passed |
| 3 | **Refused** — not testnet (RPC, Horizon or the API), the network could not be confirmed, a bad flag (a pasted `S...` seed is refused without being echoed), or an unreadable team register. Nothing was judged |
| 4 | **NO-GO** — a required check FAILED |
| 5 | **NO-GO** — nothing failed, but a required check was SKIPPED |

## 2. Record, and run the harness

Record the takes against the deployment the pre-flight passed. The lifecycle
harness (`docs/operators/lifecycle-harness.md`) produces each on-chain step as
evidence, one evidence directory per take:

```sh
python -m scripts.lifecycle --api https://orizons.xyz --agent <the external agent> \
    --intent "the intent the video types" --buyer-secret-env BUYER_1_SECRET \
    --adjudicator-key-env ORIZON_API_KEY --evidence-dir docs/evidence/5.04/take-1
```

If the pre-flight said to disclose a team wallet, say so on camera.

## 3. The evidence sheet

```sh
python -m scripts.demo_evidence docs/evidence/5.04/take-1 docs/evidence/5.04/take-2 \
    --title "Orizon Agents — Blue Belt demo (Stellar testnet)" \
    --out-dir docs/evidence/5.04/video
```

Inputs are `lifecycle.jsonl` files or the evidence directories that hold them,
read in the order given. A hash repeated across rows (a resumed run) is kept
once. `--disclose "<sentence>"` (repeatable) appends to the limitations
paragraph — use it for a team wallet the pre-flight named.

Every hash is **re-verified read-only**: RPC `getTransaction`, falling back to
Horizon for anything past the RPC's ~7-day window or when the RPC does not
answer. Only a hash that reads SUCCESS is published.

| Output | Contents |
|---|---|
| `evidence-sheet.md` | every transaction, in order: stage, what it proves, its deliverable, the hash, the Stellar Expert testnet link, the status the harness recorded and the status re-read now. Anything not SUCCESS is listed again under **Failed verification** |
| `description.txt` | ready to paste: title, a summary placeholder, a **chapters placeholder**, every verified hash with its link, and the limitations paragraph |
| `evidence.json` | the list the frontend's `/demo` page reads, in the frozen shape below; **never** a hash that failed verification |

```json
{"generated_at": 1790000000, "network": "testnet",
 "items": [{"label": "Settlement — alpha (0.0100000 XLM)", "deliverable": "D4", "kind": "settle",
            "tx_hash": "<64 hex>", "explorer": "https://stellar.expert/explorer/testnet/tx/<hash>",
            "verified": true}]}
```

| Harness event | `kind` | Deliverable |
|---|---|---|
| `register` | `register` | D1 — permissionless registration |
| `rating` | `rating` | D2 — reputation-gated routing, fed by on-chain ratings |
| `refund`, `dispute_rating` | `refund`, `dispute_rating` | D3 — dispute window and partial-credit refund |
| `authorize`, `authorize_unknown` | `authorize` | D4 — a workflow routed to an external agent and settled |
| `settle`, `charge` | `settle` | D4 |
| `seal` | `seal` | D4 |
| anything else | `other` | D4 |

A row from any network but testnet — a transaction row that does not say
`testnet`, or any row naming another network — is **refused** before a hash is
read, and nothing is written.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | every hash re-verified SUCCESS |
| 3 | **Refused** — another network (in a row, the RPC or Horizon), an unreadable input, or no transaction rows. Nothing written |
| 5 | a hash is FAILED, NOT_FOUND or malformed. Outputs written **without** it; rerun that stage or cut it from the video |
| 8 | nothing was contradicted, but a read failed, so a hash is unverified. Outputs written without it; rerun |

## 4. Upload, and fill the /demo manifest

1. Paste `description.txt` into the video's description, replacing the
   chapters and summary placeholders with the edit's timestamps.
2. Upload the video.
3. Commit `evidence.json` where the frontend's `/demo` page reads it, with the
   video's URL beside it, and the sheet with it (`docs/evidence/5.04/`).
4. Open `/demo` and click one link of each kind through to Stellar Expert.
