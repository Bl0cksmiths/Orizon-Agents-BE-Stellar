# Recording the demo video (story 5.04)

The 3–5 minute demo video shows **only real testnet transactions**, and every
one of them has to resolve on Stellar Expert. Two backend tools bracket the
recording session:

    pre-flight  →  record  →  run the harness  →  evidence sheet  →  upload  →  fill the /demo manifest

| Tool | What it answers | Reads | Writes |
|---|---|---|---|
| `python -m scripts.demo_preflight` | GO / NO-GO: is the live deployment in the state the script needs? | the deployment, the backend host's `/readiness`, Soroban RPC, Horizon, the frontend pages | nothing (one opted-in decompose aside) |
| `python -m scripts.demo_evidence` | Which transactions did the video show, and does each still verify? | the harness's `lifecycle.jsonl`, the browser's hashes (`--rows`, `--tx`), Soroban RPC, Horizon | `evidence-sheet.md`, `description.txt`, `evidence.json`; with `--index-links`, the evidence index's links |

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

### The hashes the browser produces

The video's key moments are driven **in the browser**, not by the harness: the
operator registers (S02), the buyer authorizes (S06), the step settles and is
sealed and rated (S07), and the dispute is upheld and refunded (S08–S09).
Their hashes are never in a `lifecycle.jsonl`, so collect each one **as it is
produced**, copied in full from where the frontend's shot list
(`content/demo/shot-list.md`, "Where each hash comes from") says, into one
file per session, in the order the video shows them:

```json
[
  {"kind": "register", "tx_hash": "<register_tx>", "label": "Outside operator registers research_demo1"},
  {"kind": "authorize", "tx_hash": "<authorize_tx>"},
  {"kind": "settle", "tx_hash": "<settle_tx>"},
  {"kind": "seal", "tx_hash": "<seal_tx>"},
  {"kind": "rating", "tx_hash": "<rating_tx_1>"},
  {"kind": "dispute_rating", "tx_hash": "<dispute_rating_tx>"},
  {"kind": "refund", "tx_hash": "<refund_tx>"}
]
```

Save it as `docs/evidence/5.04/browser.json` and hand it to the evidence tool
with `--rows` (§3). A hash can also be given on its own with
`--tx KIND=HASH[:label]`, for example
`--tx "register=<register_tx>:Outside operator registers research_demo1"`.

| Scene | Shot-list placeholder | `kind` | Deliverable |
|---|---|---|---|
| S02 | `<register_tx>` | `register` | D1 |
| S06 | `<authorize_tx>` | `authorize` | D4 |
| S07 | `<settle_tx>` | `settle` | D4 |
| S07 | `<seal_tx>` | `seal` | D4 |
| S07 | `<rating_tx_1>` | `rating` | D2 |
| S08/S09 | `<dispute_rating_tx>` | `dispute_rating` | D3 |
| S08/S09 | `<refund_tx>` | `refund` | D3 |

- `kind` is one of `register`, `authorize`, `settle`, `seal`, `rating`,
  `dispute_rating`, `refund`, `other`; anything else is refused.
- `tx_hash` is the 64-hex hash, or its Stellar Expert **testnet** link
  (`https://stellar.expert/explorer/testnet/tx/<hash>`). A link on any other
  network is refused.
- `label` is optional. When given it must be plain language: at least two
  real words, never a bare hash or address (the evidence index's own rule).
  If the operator is a team wallet, say so in it, for example
  `"Registration of research_demo1 by the team's QA operator key (team wallet: not external)"`.
  Without one, the kind's label is used ("Agent registration").
- `deliverable` is optional and may only restate the kind's; an `other` may
  name any of D1–D4.
- `network` is optional; anything but `"testnet"` is refused.

The S05 fault runs are harness runs: their evidence directories go to §3 as
positional inputs, as the takes' do.

## 3. The evidence sheet

```sh
python -m scripts.demo_evidence docs/evidence/5.04/take-1 docs/evidence/5.04/take-2 \
    --rows docs/evidence/5.04/browser.json \
    --title "Orizon Agents — Blue Belt demo (Stellar testnet)" \
    --out-dir docs/evidence/5.04/video \
    --index-links docs/evidence/5.04/video/index-links.json
```

| Input | Meaning |
|---|---|
| positional | `lifecycle.jsonl` files or the evidence directories that hold them (optional when `--rows` or `--tx` is given) |
| `--rows FILE.json` | the browser's hashes (§2), repeatable |
| `--tx KIND=HASH[:label]` | one browser hash, repeatable |
| `--index-links FILE.json` | also write every verified hash as an evidence index link, grouped by index item (see "Feeding story 5.05" in `docs/operators/lifecycle-harness.md`) |
| `--disclose "<sentence>"` | appended to the limitations paragraph (repeatable) — use it for a team wallet the pre-flight named |

The harness's rows come first, then the `--rows` files, then the `--tx`
hashes, each in the order given. A hash repeated anywhere (a resumed run, or a
browser hash the harness also recorded) is kept once, at its first appearance,
and counted in the sheet; the same hash given as two different kinds is
refused. A browser hash is verified exactly as a harness row is, and shows in
the sheet under the stage `browser`.

Every hash is **re-verified read-only**: RPC `getTransaction`, falling back to
Horizon for anything past the RPC's ~7-day window or when the RPC does not
answer. Only a hash that reads SUCCESS is published.

| Output | Contents |
|---|---|
| `evidence-sheet.md` | every transaction, in order: stage, what it proves, its deliverable, the hash, the Stellar Expert testnet link, the status the harness recorded and the status re-read now. Anything not SUCCESS is listed again under **Failed verification** |
| `description.txt` | ready to paste: title, a summary placeholder, a **chapters placeholder**, every verified hash with its link, and the limitations paragraph (below) |
| `evidence.json` | the list the frontend's `/demo` page reads, in the frozen shape below; **never** a hash that failed verification |

```json
{"generated_at": 1790000000, "network": "testnet",
 "items": [{"label": "Settlement — alpha (0.0100000 XLM)", "deliverable": "D4", "kind": "settle",
            "tx_hash": "<64 hex>", "explorer": "https://stellar.expert/explorer/testnet/tx/<hash>",
            "verified": true}]}
```

The limitations paragraph always states that the run is testnet only, that
every amount is testnet XLM and the interface labels it XLM (the "usdc" in API
field names is a field name), that scores are prior-smoothed, and that the
contracts are unaudited. What it says of a dispute is derived from the run:

| The run shows | The paragraph says |
|---|---|
| a verified `refund` | the dispute was upheld by the adjudicator key, and the refund is a partial credit from the platform's signing key (escrow v2's settler), not a clawback |
| … and a verified `dispute_rating` | the dispute's rating then landed on the ReputationLedger |
| … and no verified `dispute_rating` | the video claims no dispute rating |
| a verified `dispute_rating` alone | the rating landed; the video claims no refund, and no uphold |
| a dispute the harness last recorded `open` | that dispute, by id, was open when the run was recorded, with no refund or dispute rating claimed for it |
| none of these | nothing about disputes |

A refund or rating that did not re-verify SUCCESS counts as absent. `--rows`
and `--tx` carry no dispute status, so a dispute opened in the browser and
never adjudicated is not mentioned; say so with `--disclose`.

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
| 3 | **Refused** — another network (in a row, a `--rows` row, a `--tx` link, the RPC or Horizon), an unreadable input, a `--tx` or `--rows` row with an unknown kind, a bad label or a bad deliverable, one hash given as two kinds, or no transaction rows. Nothing written |
| 5 | a hash is FAILED, NOT_FOUND or malformed. Outputs written **without** it; rerun that stage or cut it from the video |
| 8 | nothing was contradicted, but a read failed, so a hash is unverified, or (with `--index-links`) a verified hash's ledger date could not be read from Horizon. Outputs written without it; rerun |

## 4. Upload, and fill the /demo manifest

1. Paste `description.txt` into the video's description, replacing the
   chapters and summary placeholders with the edit's timestamps.
2. Upload the video.
3. In the **frontend** repo, fill `content/demo/demo.json`, the manifest the
   `/demo` page is built from. `evidence.json` is not read as a file anywhere:
   its whole object, `{"generated_at", "network", "items"}`, is pasted
   **verbatim** as the value of the manifest's `evidence` key, beside `status`
   (`"published"`), `video` and `chapters`:

   ```json
   {
     "status": "published",
     "video": {"provider": "...", "id": "...", "title": "...", "duration_seconds": 240, "published_at": "..."},
     "chapters": [...],
     "evidence": {"generated_at": 1790000000, "network": "testnet", "items": [...]},
     "transcript_file": "...",
     "captions_file": "..."
   }
   ```

   Run `npm run demo:check` there: it refuses a manifest whose evidence is not
   testnet, has an item that is not `verified: true` or whose `explorer` is
   not the testnet page for its own hash, or lacks at least one `settle`, one
   `dispute_rating` and one `refund` — the browser's hashes (§2), so give them
   to the tool. Commit it on the frontend.
4. Commit the sheet, `description.txt` and `evidence.json` in the backend repo
   under `docs/evidence/5.04/video/`, beside `browser.json` and the takes'
   evidence directories (never their `state.json`).
5. Merge `index-links.json` into the frontend's `content/evidence/index.json`,
   as "Feeding story 5.05" in `docs/operators/lifecycle-harness.md` describes.
6. Open `/demo` and click one link of each kind through to Stellar Expert.
