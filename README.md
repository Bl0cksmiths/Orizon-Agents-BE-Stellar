# Orizon Agents — Backend

FastAPI + Agno + OpenAI. The brain behind the Orizon Agents frontend.

## 🚀 Live deployment

| layer | live URL | source |
| --- | --- | --- |
| ⚙️ **Backend** (this repo, Render) | **https://orizon-agents-be-stellar.onrender.com** | this repo |
| 🌐 **Frontend** (Vercel) | **https://orizon-agents-fe-stellar.vercel.app** | [Frontend repo](https://github.com/ALGOREX-PH/Orizon-Agents-FE-Stellar) |
| 🔗 **Soroban contracts** | 4 contracts deployed on Stellar **mainnet** + testnet | [Contracts repo](https://github.com/ALGOREX-PH/Orizon-Agents-Smart-Contract-Stellar) |

**Verify it's live:** `curl https://orizon-agents-be-stellar.onrender.com/api/stellar/network` — returns the four contract IDs the FE renders (mainnet in production via `render.yaml`; testnet is the local-dev default).

**▸ Try the full flow:** [open the dApp](https://orizon-agents-fe-stellar.vercel.app/app/orchestrator) → connect [Freighter](https://freighter.app) on **Test Net** → type `code a calculator web app` → **Authorize & Execute**.

## Setup

```bash
# install uv (once) — https://docs.astral.sh/uv/
curl -LsSf https://astral.sh/uv/install.sh | sh

# create venv + install deps
uv venv .venv
uv pip install -r requirements.txt

# configure
cp .env.example .env
# edit .env: set OPENAI_API_KEY

# run
./run.sh
# → http://localhost:8000  (docs at /docs)
```

## Endpoints

| method | path | purpose |
| --- | --- | --- |
| GET  | `/health`                            | liveness probe |
| GET  | `/api/health`                        | the same liveness probe under the `/api` prefix |
| GET  | `/readiness`                         | readiness probe — per-dependency config status, plus two informational blocks: `cold_start` (can a brand-new agent clear the floor?) and `ratings` (can this deployment write ratings — is its signer the ledger's scorer?) |
| GET  | `/api/agents`                        | registry listing |
| GET  | `/api/agents/{id}`                   | agent detail |
| POST | `/api/orchestrator/decompose`        | intent → plan (real LLM) |
| POST | `/api/orchestrator/execute`          | run a plan → `{task_id}` |
| GET  | `/api/tasks`                         | recent tasks |
| GET  | `/api/tasks/{id}`                    | task detail |
| GET  | `/api/tasks/{id}/artifact`           | task deliverable (the actual artifact) |
| GET  | `/api/trace/{task_id}`               | full trace snapshot |
| GET  | `/api/trace/{task_id}/stream`        | SSE live trace |
| GET  | `/api/metrics/overview`              | dashboard overview |
| GET  | `/api/flow/default`                  | default DAG |
| POST | `/api/payments/x402`                 | simulated HTTP 402 flow |
| GET  | `/api/stellar/network`               | configured-network contract IDs the FE renders |
| GET  | `/api/stellar/agent/{id}`            | read an agent from AgentRegistry |
| GET  | `/api/stellar/reputation`            | smoothed reputation for every agent + routing floor |
| GET  | `/api/stellar/reputation/params`     | full reputation parameter set — priors, floor, decay constants |
| GET  | `/api/stellar/reputation/{id}`       | smoothed reputation for one agent |
| GET  | `/api/stellar/attestation/{job_id}`  | on-chain attestation by hex job id |
| POST | `/api/stellar/build/register-agent`  | unsigned XDR — owner signs via Freighter |
| POST | `/api/stellar/build/authorize`       | unsigned XDR — x402 pre-auth |
| POST | `/api/stellar/submit`                | submit a Freighter-signed XDR |
| POST | `/api/stellar/server/charge`         | backend-signed escrow charge (needs `X-API-Key`) |
| POST | `/api/stellar/server/seal`           | backend-signed attestation seal (needs `X-API-Key`) |
| GET  | `/api/stellar/new-id`                | fresh random 16-byte id for job/auth ids |
| POST | `/api/disputes/challenge`            | mint the exact message + nonce the payer's wallet signs to dispute a step |
| POST | `/api/disputes`                      | open a dispute on a settled step — authorized by that signature, no API key |
| GET  | `/api/disputes/{id}`                 | read one dispute by the unguessable id opening it returned |
| GET  | `/api/tasks/{id}/disputes`           | a task's dispute window (`window_closes_at`) and every dispute raised on it |
| POST | `/api/disputes/{id}/uphold`          | adjudicate in the buyer's favour and pay the credit, settler → buyer (needs `X-API-Key`, and **refuses** while it is unset) |
| POST | `/api/disputes/{id}/reject`          | adjudicate against the claim — records the verdict, signs nothing (needs `X-API-Key`, and **refuses** while it is unset) |
| *    | `/api/pdax/*`                        | PDAX PHP↔crypto surface: trade, fiat/crypto funding, ramps, webhooks, reference data |

`/api/health` exists because the frontend reaches this API only through a same-origin rewrite of `/api/*` — the root `/health` sits outside that prefix, so mirroring it under `/api` is what lets the browser and any external uptime monitor pointed at the product domain verify the backend is actually reachable. It returns the identical payload, makes no network or contract calls, and is exempt from rate limiting and access logging just like the root probe.

## Reputation system

Raw reputation evidence lives on-chain, aggregation lives here (the ERC-8004 split). The **ReputationLedger v2** contract stores decayed, value-weighted rating evidence per agent: every rating is weighted by the USDC **at stake** on the step that earned it, old evidence decays each epoch, and submissions are scorer-gated with a kind of `auto` (settler), `buyer`, or `dispute`. Reputation is a record of economic exposure, not a count of clicks.

The weight is the step's *quoted* price, not money that changed hands — and the distinction is load-bearing rather than pedantic. A failed step is never billed yet is rated all the same, so weighting by settled value would make every negative rating weightless: non-delivery settles nothing. Weighting by what was at stake is as true of a step that failed as of one that delivered, which is what lets non-delivery carry a cost at all. See `app/services/reputation_svc.py`'s module docstring and [docs/reputation.md](docs/reputation.md).

The backend turns that evidence into routing decisions. A Bayesian prior (default 7000 bps = 3.5/5) smooths sparse evidence so permissionless newcomers start at a meaningful score instead of zero, and a Wilson-style lower bound on the smoothed mean feeds the routing floor: at decompose time, agents whose bound falls below `REPUTATION_FLOOR_BPS` are omitted from the planner's registry (never shrinking the candidate list below 3), and every plan step is stamped with the live smoothed score (`rep_bps` / `rep_source`). If the chain is unreachable the caller gets the prior, marked `source="prior"` — reads never fail.

Each plan step also carries the rest of the reputation the floor was judged on, from the same snapshot, so a plan card never needs a second request (all optional, so older plans and clients still validate):

| `PlanStep` field | meaning |
| --- | --- |
| `rep_lower_bound_bps` | the conservative bound the routing floor is applied to — the number that decided routability |
| `rep_count` | lifetime rating count; `0` with `rep_source="prior"` and `rep_degraded` false is a genuine cold start |
| `rep_dispute_rate_bps` | share of those ratings that were disputes |
| `rep_degraded` | the agent's on-chain read **failed** and the prior was served, so the numbers above are an estimate |
| `degraded` | the step was **re-admitted below the floor** by the starvation backstop — a verdict, unrelated to `rep_degraded` |

The `rep_*` fields are absent (`null`, `rep_degraded` `false`) only for an agent with no reputation entry at all.

The plan as a whole says what shaped it, on both planning paths (all defaulted, so an older client still validates):

| `DecomposeResponse` field | meaning |
| --- | --- |
| `notices` | every floor action taken while building the plan — exclusions, substitutions, starvation-backstop re-admissions — plus on-chain agents excluded for having no bound endpoint |
| `floor_bps` | the routing floor this plan was actually judged against, read from settings at plan time |
| `reputation_degraded` | at least one reputation read in this plan's snapshot **failed** and the prior was served, so the floor verdicts rest on an estimate |
| `planner_fallback` | the steps are the deterministic **fallback plan**, not the planner's own — the planning model failed or returned no usable plan, or every step it chose was clamped away. Still stored, executable and held to the floor like any plan; always `false` on the demo-kit path |

A free-form intent does not fail because the model did. A blank `OPENAI_API_KEY`, a refused connection or an upstream error is served the fallback plan with `planner_fallback: true`; the provider's message is logged with any API key redacted, and never returned. Two planning conditions still refuse the request: a planner that outlives `DECOMPOSE_TIMEOUT_SECONDS` (504 `decompose_timeout`), and nothing listed and dispatchable left to route to (503 `no_routable_agents`) — checked before any LLM call is spent, and again before the fallback is built.

After each settled workflow the settler submits one synthetic rating per step (`kind="auto"`), derived from verifiable workflow signals — did the worker deliver output, ship an artifact, trip critic violations — so scores are validation-gated rather than opinion. Submissions run sequentially (one scorer account) and are best-effort: a failed rating logs a trace line and never fails the workflow.

Read it via `GET /api/stellar/reputation` (all agents + floor/prior) or `GET /api/stellar/reputation/{id}` (one agent). Tunables:

| name | default | purpose |
| --- | --- | --- |
| `REPUTATION_ENABLED` | `true` | master switch for on-chain rep reads + settler rating submission |
| `REPUTATION_PRIOR_BPS` | `7000` | prior mean, in bps of the 0–100 rating scale (7000 = 3.5/5) |
| `REPUTATION_PRIOR_WEIGHT_USDC` | `12` | evidence mass of the prior — settled USDC needed for evidence to dominate |
| `REPUTATION_FLOOR_BPS` | `5500` | routing floor applied to the smoothed lower bound at decompose time |
| `REPUTATION_READ_TTL_SECONDS` | `15` | TTL for cached on-chain `rep_state` reads, per agent |
| `REPUTATION_MAX_RATING_WEIGHT_USDC` | `100` | per-rating weight cap — one whale job can't own the score |

`REPUTATION_PRIOR_BPS`, `REPUTATION_PRIOR_WEIGHT_USDC` and `REPUTATION_FLOOR_BPS` between them decide whether a brand-new agent is routable at all, and the margin is 177 bps. **[docs/reputation.md](docs/reputation.md)** has the arithmetic, the exact value at which each one starts excluding newcomers, and why the floor is applied to the lower bound rather than to the raw on-chain mean — read it before changing any of them.

## Dispute window

A settled workflow can be argued with. When a paid workflow settles, the settlement is recorded — the job id, the payer, what each step was actually charged — and stamped with a closing time `DISPUTE_WINDOW_SECONDS` (24 h) ahead of it. Until that moment the buyer may dispute any step that was charged: `POST /api/disputes/challenge` returns the exact string to sign, the wallet that paid signs it, and `POST /api/disputes` records the claim. There is no account and no API key anywhere in that flow — **the wallet signature is the credential**, exactly as it is for endpoint binding, because the only thing that needs proving is "I am the address that paid this job", and a shared operator key cannot say that. It would be the wrong key besides: the operator is the party being disputed.

The deadline is stamped on the settlement record rather than recomputed on read, so retuning `DISPUTE_WINDOW_SECONDS` can never move a closing time a buyer was already given; it only applies to workflows that settle afterwards. One dispute per `(job, step)`: a repeat is answered with the original dispute unchanged, not a second record. `GET /api/tasks/{id}/disputes` returns the window and everything raised on a task, which is what the console shows while the clock runs.

Story 4.02 records the claim and nothing more — no money moves and no rating is written. Paying the credit is 4.03 (`DISPUTE_CREDITED_FRACTION`, default the whole step) and the on-chain `kind="dispute"` rating is 4.04.

## Testing

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest
```

489 tests, all hermetic — no OpenAI key, no network, no funded Stellar account needed. `ruff check`, `ruff format --check`, `mypy` (strict-defs), and an 82% coverage floor guard the suite; CI runs all of them on every push and PR, and `make check` runs the same gate locally.

## Environment variables

| name | default | purpose |
| --- | --- | --- |
| `API_KEY` | *(unset)* | when set, `/api/stellar/server/*` and all non-public `/api/pdax/*` routes require a matching `X-API-Key` header |
| `TASK_AUTH_REQUIRED` | `false` | when true, task/trace/artifact reads require the per-task `read_token` returned by execute |
| `ORCHESTRATOR_MAX_CONCURRENT` | `8` | in-flight workflow ceiling — excess execute calls get a 503 `capacity_exhausted` |
| `RATE_LIMIT_PER_MINUTE` | `1200` | request budget per resolved client key (sliding 60 s window) — see below |
| `TRUSTED_PROXY_HOPS` | `0` | how many **trailing** `X-Forwarded-For` entries are this deployment's own infrastructure and are skipped when resolving the client |
| `FORWARDED_CHAIN_SAMPLES` | `5` | log the raw forwarded chain + resolved key for the first N non-exempt requests after each restart (`0` disables) |
| `MAX_CHARGE_USDC` | `100` | server-side ceiling for a single `PaymentEscrow.charge`, in USDC |
| `DOCS_ENABLED` | `true` | serve `/docs`, `/redoc`, and `/openapi.json` |

Everything else (model IDs, contract addresses, RPC, PDAX sandbox) is documented in `.env.example` — copy it to `.env` and fill in what you need.

### Rate limiting and the proxy trust boundary

`X-Forwarded-For` is append-only — each proxy adds the address of the peer it received the request from — so the chain reads `<anything the caller sent>, <caller's address>, <our edge>, …` and only the **rightmost** entries were written by infrastructure we control. `TRUSTED_PROXY_HOPS` says how many of those trailing entries are ours; `client_key()` (`app/security.py`) drops them, and the entry to their left becomes the rate-limit bucket and the access log's `client=` field.

**The budget is currently effectively global.** The default of `0` drops nothing and keys on the last entry, which is the address this deployment's own edge appends — the same value for every visitor. `RATE_LIMIT_PER_MINUTE` therefore behaves as one budget for the whole service rather than one per visitor, and `client=` is a constant in every access line, so abuse cannot be attributed during an incident. The default is `0` on purpose: it reproduces the behaviour this service has always had, so nothing changes until the hop count is tuned against a chain actually observed from production. The limit is sized for that reading — an open dashboard tab polls two endpoints every 5 s (24 req/min), so `1200` seats roughly 50 concurrent tabs, where the old `120` seated five. Both liveness probes are exempt, and the frontend backs off on `429` and honours `Retry-After`.

Setting it **too high** is the worse failure and is equally silent: the resolved entry becomes one the *caller* wrote, so anyone can mint a fresh bucket per request by rotating a header value and the limiter stops limiting. A chain too short to contain a client entry resolves to the literal `forwarded-chain-too-short` rather than clamping to the leftmost (caller-controlled) entry — seeing that as `client=` means the value is set higher than the chain this edge actually produces.

**Verifying the hop count from logs.** The number of entries Vercel's rewrite proxy and Render's edge each contribute is not observable from outside, so read it off production. After any restart (changing an env var in the Render dashboard triggers one), the first `FORWARDED_CHAIN_SAMPLES` non-exempt requests each log one line to Render's log stream:

```
forwarded chain sample 1/5 on GET /api/agents: entries=3 chain=['203.0.113.50', '76.76.21.9', '10.201.3.4']
peer=203.0.113.50 TRUSTED_PROXY_HOPS=0 resolves client=10.201.3.4 — set TRUSTED_PROXY_HOPS to the number of
TRAILING entries this edge appends, so the one to their left is the visitor
```

Load the console in a browser, then read one sample line: count the trailing entries that are *not* the visitor (Render's edge, plus Vercel's egress if the request came through `orizons.xyz`) and set `TRUSTED_PROXY_HOPS` to that count. Confirm by watching `client=` in the access lines vary between visitors instead of repeating. The sample line and the access line for the same request share an `X-Request-ID`, so they can be read side by side. This is deliberately a log sample and not a diagnostic endpoint — the chain contains visitors' IP addresses, and `API_KEY` is empty on demo deployments, so a route could not be reliably gated; after the budget is spent the cost is a single integer comparison per request.

## Deploy — Render (recommended)

The repo ships a `render.yaml` blueprint + a `runtime.txt` pinning Python 3.12. Render reads them on first connect.

1. Push to GitHub:
   ```bash
   git add render.yaml runtime.txt app/main.py README.md
   git commit -m "chore: render deploy"
   git push origin main
   ```
2. Go to [render.com](https://render.com) → **New → Blueprint** → connect `Orizon-Agents-BE-Stellar`.
3. Render detects `render.yaml` and lists two secrets you must fill (`sync: false`):

   | name | value |
   | --- | --- |
   | `OPENAI_API_KEY` | your OpenAI key (secret) |
   | `STELLAR_SIGNING_KEY` | your admin `S…` secret — optional, needed only for real on-chain charge/seal |

   All other env vars (model IDs, contract addresses, RPC) are preset in `render.yaml`.

4. Click **Apply**. First build takes ~2–3 minutes. You'll get `https://orizon-agents-be-xxxx.onrender.com`.
5. After the frontend is deployed, update `CORS_ORIGINS` in the Render dashboard to the production domains (`https://orizons.xyz` and `https://www.orizons.xyz`) plus your Vercel URL. Render redeploys automatically (~30 s).
6. (Optional) Register the on-chain `orizon_batch` agent so the Authorize & Execute flow can settle:
   ```bash
   cd ~/Websites-Services-2026/orizon-agents-BE-Stellar
   .venv/bin/python scripts/register_batch_agent.py
   ```
   One-time tx; runs against whichever contract addresses are in your `.env`.

### Mainnet

The contracts are live on Stellar **mainnet** — `render.yaml` ships these as the production env (testnet stays the local-dev default in `.env.example`):

| contract | mainnet ID |
| --- | --- |
| AgentRegistry | [`CBTJ3BXTMTA2PQLRTSAZHEWQRTBMNHYCOKY5WOIYAH36LT4HTN63LTD4`](https://stellar.expert/explorer/public/contract/CBTJ3BXTMTA2PQLRTSAZHEWQRTBMNHYCOKY5WOIYAH36LT4HTN63LTD4) |
| ReputationLedger | [`CDFWQJY72GPH7PEQVFGBDZESZNVRF6LQLVWU42CFMWPGRME5RWN5AXSX`](https://stellar.expert/explorer/public/contract/CDFWQJY72GPH7PEQVFGBDZESZNVRF6LQLVWU42CFMWPGRME5RWN5AXSX) |
| PaymentEscrow | [`CBJCQBA47Q3EQ7HC46GAWJPVM7KMD5KAEI5KG4FPYJFKR3NYB4QR5CNF`](https://stellar.expert/explorer/public/contract/CBJCQBA47Q3EQ7HC46GAWJPVM7KMD5KAEI5KG4FPYJFKR3NYB4QR5CNF) |
| AttestationRegistry | [`CBLV6QGFCMXBXHT62JZ7YH22NXW7MVBGV6TGOGX3OHY46GQGPYCTAAK4`](https://stellar.expert/explorer/public/contract/CBLV6QGFCMXBXHT62JZ7YH22NXW7MVBGV6TGOGX3OHY46GQGPYCTAAK4) |
| XLM SAC (native, SEP-41) | [`CAS3J7GYLGXMF6TDJBBYYSE3HQ6BBSMLNUQ34T6TZMYMW2EVH34XOWMA`](https://stellar.expert/explorer/public/contract/CAS3J7GYLGXMF6TDJBBYYSE3HQ6BBSMLNUQ34T6TZMYMW2EVH34XOWMA) |

**Go live:** the Render dashboard env overrides `render.yaml` — flip the dashboard's Stellar vars to the `render.yaml` values and the service redeploys on mainnet.

### Gotchas

- **Free-tier sleep**: Render's free plan sleeps after 15 min idle. First request after idle takes ~30–50 s. Upgrade to Starter ($7/mo) for always-on.
- **SSE**: trace streams work fine for Orizon's ~4 s workflows. For long-lived streams (> 5 min), Render's free-plan buffer can cut them — move to paid or hit the backend directly.
- **Never commit** `OPENAI_API_KEY` or `STELLAR_SIGNING_KEY`. They live only in Render's dashboard; `.env` stays gitignored.

## Notes

- Rate-limited (non-exempt) responses carry `X-RateLimit-Limit` / `X-RateLimit-Remaining`; throttled requests get `429` + `Retry-After`.
- Every response carries hardening headers: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`.
- Every response echoes an `X-Request-ID` (yours, or a generated one). Logs leave as single-line JSON — every record (access line and service logs alike) carries the request id, so a 500 and its traceback correlate.
- Error responses share one envelope: the legacy `detail` plus `error: {code, message, request_id}` — the same shape for 4xx, validation errors, 429s, and 500s.
- **Durability**: storage is in-memory by design — task history, traces, and PDAX ramp records reset on every restart (Render's free tier idles out routinely). Durable facts live on-chain. Do not run real-money PDAX ramps on this deployment; move ramp state to a persistent store first (that project pairs naturally with going multi-worker).
- Public-demo scope: task history (`/api/tasks`, traces, artifacts) is world-readable **by default** so visitors can watch runs. Capability-token auth is fully wired — every execute response returns a `read_token`, and setting `TASK_AUTH_REQUIRED=true` enforces it on task/trace/artifact reads (the token rides an `X-Task-Token` header, or `?token=` for SSE; a valid `X-API-Key` bypasses for ops). Flip the env var when real users bring real intents.
- `/docs`, `/redoc`, and `/openapi.json` are public on purpose — this is a showcase API. Set `DOCS_ENABLED=false` to turn them off.
- 4 real Agno workers (`copywrite.v3`, `seo.brief`, `research.pro`, `sol-audit`) + `code.gen`; the remaining workers are mocks.
- Payments and ERC-8004 proofs are simulated unless `STELLAR_SIGNING_KEY` is set — then they become real testnet transactions.
