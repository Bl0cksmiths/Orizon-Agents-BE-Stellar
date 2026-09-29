# Measuring the SOW §6.3 success metrics (story 5.05)

The evidence index states each of the eleven SOW §6.3 metrics with its target,
the value achieved, how it was measured and links to the proof, and says
plainly why a target was missed. None of those values is typed in by hand:
`python -m scripts.sow_metrics` measures every one from Stellar testnet and
the live deployment, the same way every time.

```sh
source .venv/bin/activate
python -m scripts.sow_metrics --out-dir docs/evidence/5.05/metrics
```

It is **testnet only** and **read-only**: every call is a GET or a contract
simulation, retried a bounded number of times, and it needs no secret. It
signs, submits and deploys nothing. A run takes about a minute (most of it is
Render waking up and one simulation per escrow id).

| Flag | Default | Meaning |
|---|---|---|
| `--api` | `https://orizons.xyz/api` | the deployed API (a trailing `/api` is accepted) |
| `--frontend` | `https://orizons.xyz` | the deployed frontend, for the register, guide and demo pages |
| `--backend` | `https://orizon-agents-be-stellar.onrender.com` | the backend's own host, for `/readiness` and `/openapi.json`, which `orizons.xyz` does not proxy |
| `--github-api` | `https://api.github.com` | where the repository licences are read (unauthenticated; four calls) |
| `--escrow C...` | — | another escrow contract to count, besides the live one and the known v1 (repeatable) |
| `--team-register` | `app/data/team_wallets.json` | the committed register of team wallets |
| `--pending-link ID=URL[=label]` | — | for milestone `m06`, `m09` or `m10`: when its page answers 404, link this GitHub pull request (`https://github.com/<owner>/<repo>/pull/<n>`) in place of the page (repeatable, once per milestone). The label is optional; without one it reads "Pull request #n in owner/repo that adds the /page page (not deployed yet)". A label must be at least two words. Ignored, with a note, when the page does not answer 404 |
| `--rpc-url`, `--horizon-url` | the public testnet endpoints | |
| `--out-dir` | — | write the three outputs below |
| `--print-block` | off | also print the frozen block to stdout |

## Outputs

| File | What it is |
|---|---|
| `sow-metrics.block.json` | **The frozen metrics block.** The frontend's `content/evidence/index.json` `metrics` array is pasted from it, unchanged. |
| `sow-metrics.md` | The same eleven rows as a Markdown table, each with why it was missed, how it was measured and its proof links. |
| `sow-metrics.raw.json` | Every counted and excluded item per metric with its raw addresses and hashes, the sources read, every read failure and warning, and a summary (agents, each escrow's ids, ratings by kind, platform transfers). |

The block is an array of eleven entries, `m01`..`m11` in SOW order:

```json
{ "id": "m04", "category": "Transaction targets",
  "metric": "On-chain USDC settlements (charges) recorded", "target": "≥ 3",
  "achieved": "0", "status": "not_met",
  "reason": "None of the 8 charges on record counts: ...",
  "method": "Walked every id each escrow contract has issued ...",
  "links": [{ "label": "Charge of 0.114 XLM to orizon_batch on the v1 escrow — 2026-05-13 (excluded: ...)",
              "url": "https://stellar.expert/explorer/testnet/tx/<hash>", "kind": "tx",
              "tx_hash": "<hash>", "date": "2026-05-13" }] }
```

- `category`, `metric` and `target` are the SOW's words, character for character.
- `status` is `met` or `not_met`. `reason` is present exactly when the status is `not_met`, in plain language.
- Every link has a plain-language `label` — never a bare hash or address — an https `url` and a `kind`
  (`tx`, `contract`, `account`, `page`, `pr`, `repo`, `video`, `doc`). Explorer links are exactly
  `https://stellar.expert/explorer/testnet/tx/<hash>`, `…/contract/C…` or `…/account/G…`; a `tx` link also
  carries its `tx_hash` and `date` (`YYYY-MM-DD`).
- **No dead links.** A milestone page (`/app/register`, `/guide/list-your-agent`, `/demo`) that answers 404 is
  not linked: a link that 404s proves nothing, and the evidence index cannot take it. With `--pending-link`
  the pull request that adds the page is linked instead (`kind: "pr"`); without it the row links nothing for
  that page. Either way its `method` ends by saying so.
- The block carries no timestamp: two runs against an unchanged chain and deployment write byte-identical
  blocks, so a diff of the block is a diff of the evidence.

The generator checks this shape before it writes anything, and refuses (with a
traceback) to write a block that breaks it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every metric was measured — **whatever it came to**. A measured miss is a successful measurement. |
| 3 | Refused: not testnet (the RPC's `getNetwork`, Horizon's root document and the API's `/api/stellar/network` must all say testnet), a bad flag, or an unreadable team register. Nothing is measured or written. |
| 4 | A read failed, so at least one metric is **Not measured**. Its `achieved` is `"Not measured"` — never `0` — its status is `not_met`, and its reason names what could not be read. Run it again. |

## How each metric is measured

**Who is external.** An owner is external only if it is neither in the
committed team register (`app/data/team_wallets.json`) nor a key the platform
runs: the network admin and dispatch signer (`/api/stellar/network`), the
ratings signer and scorer (`/readiness`), the registry admin (`admin()`), each
escrow's settler and admin, the reputation ledger's admin and scorer, and the
attestation registry's admin and sealer (all read from the contracts' own
instance storage). If any of those reads fails, every count that depends on
them is Not measured: an owner that cannot be ruled out is not counted.

| # | Rule |
|---|---|
| m01 | Every agent `AgentRegistry.list_ids()` lists, with its owner from `get(id)`; counted when the owner is external. Each registration links its transaction (found in the owner's Horizon history). |
| m02 | The distinct owners of the registered agents, each classified the same way. |
| m03 | A workflow is a distinct settled job with at least one counted charge (m04) to an externally owned agent. Two payouts of one job are one workflow. |
| m04 | Every receipt each escrow has issued is one charge: a v1 `charge`, or one payout of a v2 `settle`. The escrow's instance `Nonce` numbers every authorization and receipt it has ever issued, so ids `0..Nonce-1` are its complete history, read by `receipt(id)` / `authorization(id)`, independent of how long the RPC keeps events. A charge is **excluded** when it is a self-payment (the payer owns the agent, is the escrow's settler, or is a platform key) or settled before the sprint began (2026-09-07). The live escrow is read, plus the known v1 escrow (so its history is still counted after the move to v2), plus any `--escrow`. v2 answers `version()` with 2; v1 has no `version()`. |
| m05 | A dispute refund counts only when it traces to a real dispute: a `kind=dispute` rating (from the platform keys' Horizon history of `ReputationLedger.submit`) whose job id is the derived dispute id of a counted charge's job (`job_id[:8] ‖ sha256(job_id ‖ "orizon-dispute:v1" ‖ step)[:8]`, `app/services/dispute_rating.py`), followed by an asset-contract transfer from a platform key to that charge's payer, after the charge and no larger than it. A transfer with no dispute behind it — the 4.01 drill to a team key — is excluded. The ledger's lifetime `rep_state(id).disputed` is read as a cross-check: if the ledger counts more disputes than the history shows, m05 is Not measured rather than 0. |
| m06 | `/app/register` answers 200 with no login (redirects are not followed), and the live backend's `/openapi.json` publishes `POST /api/stellar/build/register-agent`. The most recent registration signed by a key other than the registry admin is linked. |
| m07 | `/api/stellar/reputation/params` says `"enabled": true` with a positive `floor_bps`. |
| m08 | Met (`Yes`) when the live backend publishes the dispute routes (open, read, uphold, reject), **and** a dispute window can open, **and** `/readiness` reports `disputes.reconcile.enabled: true` (true only when `DISPUTE_REFUNDS_ENABLED` and `REFUND_RECONCILE_ENABLED` are both on; a readiness report without the field predates the refund switch), **and** m05 found at least one real refund. A dispute window opens only when a payment settles, and only escrow v2 settles one (v1's `charge` cannot move a payer's funds, D-039): so it can open only while the live escrow is v2, and has opened only once a v2 escrow holds a receipt. `achieved` is `No` when a dispute route is missing. When the routes are deployed but anything else fails, it is `Partly: the dispute routes are deployed, but …` naming each gap, and never says the window is live. Before v2 with refunds off it reads exactly: `Partly: the dispute routes are deployed, but no dispute window can open until a payment settles, which needs escrow v2, and refunds are switched off.` "No refund yet" is named only when a window could have opened. |
| m09 | `/guide/list-your-agent` answers 200 with no login. A 404 is linked as its `--pending-link`, or not at all. |
| m10 | `/demo` answers 200 with no login, its article carries `data-demo="published"` (rendered by the frontend's `components/demo/demo-article.tsx` from `content/demo/demo.json`'s `status`), and the running time it shows (`<time dateTime="PT3M42S">`) is 3 to 5 minutes inclusive. A 404 is linked as its `--pending-link`, or not at all. |
| m11 | GitHub's API detects `license.spdx_id == "MIT"` on each of `Bl0cksmiths/Orizon-Agents-FE-Stellar`, `-BE-Stellar`, `-Smart-Contract-Stellar` (the SOW's repositories) and `Orizon-Agents-Example-Agent-Stellar`. A `LICENSE` file GitHub does not detect, or metadata in `Cargo.toml`, does not count. |

"USDC" in the SOW's wording settles as native XLM on testnet: the escrow's
payment asset is the native XLM asset contract. The block says so in m03 and
m04's method.

## Limits, stated

- Transaction hashes for proof links, dispute ratings and refunds come from Horizon's operation history of
  the platform keys (and, for registrations, of each owner). A refund paid from a key that is neither in the
  register nor a runtime key would not be seen.
- A team QA buyer paying an outside operator's agent counts as a settlement: the rule excludes self-payment,
  not team membership, because the money really moves between two parties. It would still show in the raw
  JSON with its payer.
- The generator counts what the chain holds; it cannot see a dispute record that never reached the ledger.

## Refreshing the evidence index

1. Run the generator with `--out-dir`, and a `--pending-link` for each milestone page that is not deployed yet,
   for example
   `--pending-link m09=https://github.com/Bl0cksmiths/Orizon-Agents-FE-Stellar/pull/97`. Exit 0 means every
   row was measured.
2. Paste `sow-metrics.block.json` as the `metrics` array of the frontend's `content/evidence/index.json`.
3. Keep `sow-metrics.raw.json` next to it: it is the answer to "which items were excluded, and why".

The contract tests (`tests/test_sow_metrics_contract.py`) pin every backend
fact the generator re-states — the derived dispute id, the routes, the
readiness field, the network and reputation documents — so a backend change
that would make a metric read the wrong thing fails CI.
