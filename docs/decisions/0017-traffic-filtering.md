# ADR 0017 — /api answers our frontend: traffic filtering in three layers

- **Status:** Accepted, 2026-10-10
- **Deciders:** Danielle (lead)
- **Builds on** the frontend proxy identity (`FRONTEND_PROXY_TOKEN`,
  `security.is_frontend`, README "Rate limiting and who the client is") and
  the rate limiters it keys (`app/security.py`, `app/rate_limit.py`).

## Context

`https://orizons.xyz` is the product's only advertised entry point. Vercel's
firewall sits in front of it, and the frontend's Next.js middleware
(`middleware.ts`, matcher `/api/:path*`) forwards every `/api/*` request to
this service with `X-Frontend-Proxy-Token`, after stripping any copy the
browser sent.

Render's own hostname is just as public. Checked read-only on 2026-10-10:

| Request, straight to `orizon-agents-be-stellar.onrender.com` | Status |
| --- | --- |
| `GET /docs` | 200 |
| `GET /openapi.json` | 200 |
| `GET /api/agents` | 200 |

So anyone could skip the firewall — and with it every edge rule, bot check and
rate limit Vercel applies — by calling Render directly, and `/openapi.json`
told them every route to call. The only thing that held them was this
service's own rate limiters, which were built to share budgets fairly between
visitors, not to be the outer wall.

## Decision

Three layers, outermost first, with the rate limiters kept as the inner
fourth.

### T1 — Vercel WAF at the edge

Custom rules and bot management on the `orizons.xyz` project filter traffic
before it reaches a function or the rewrite. This is the frontend lane's work
and configuration; nothing in this repository changes for it.

### T2 — The origin lock (this repository)

`OriginLockMiddleware` (`app/origin_lock.py`) checks every request whose path
starts with `/api/`. One carrying the frontend token (`security.is_frontend`,
constant time) passes. Any other is **locked out**, unless the allowlist
below admits it. What happens to a locked-out request is `ORIGIN_LOCK_MODE`:

- `off` — nothing.
- `log` (the default) — served. It is counted, and logged as one WARNING
  per route template per minute: method, template (never the path or query
  string, which can carry ids and `?token=`), the rate limiters' client
  identity and the request id, with how many were folded into the line.
  `/readiness` reports `origin_lock: {mode, would_block_total,
  would_block_last_hour}` from memory.
- `enforce` — answered `403` in the unified envelope, code
  `origin_forbidden`, message "This API is only available through
  orizons.xyz.", with `Cache-Control: no-store`. Nothing the caller sent is
  echoed. Config refuses to boot `enforce` without `FRONTEND_PROXY_TOKEN`:
  with no token nothing could pass, and a lock that refuses its own frontend
  is an outage.

Not locked at all: paths outside `/api/` (`/`, `/health`, `/readiness` and the
docs, which `DOCS_ENABLED` governs), and `OPTIONS` (a preflight cannot carry
the token; CORSMiddleware answers it before the lock in any case).

**Where it sits.** Starlette runs middleware in reverse order of
`add_middleware`. The lock is added after `RateLimitMiddleware` and before
`SecurityHeadersMiddleware`, so the stack, outermost first, is:

`RequestContext → GZip → CORS → SecurityHeaders → OriginLock → RateLimit → RouteRateLimit → BodyLimit → router`

- Outside both limiters and the body cap: a refusal costs a header compare.
  It spends no visitor's budget, so a flood of direct calls cannot 429 the
  frontend's shared buckets, and no body is read.
- Inside RequestContext: the log line and the 403 carry the request id.
- Inside SecurityHeaders and CORS: the 403 carries the hardening headers and,
  for an allowed origin, the CORS header a browser needs to read it.

### T3 — BotID on the money and AI routes (frontend)

The frontend verifies Vercel BotID on the routes that spend money or model
tokens (execute, decompose, the dispute and binding writes) before forwarding
them. A bot that passes the WAF still has to pass this before it costs
anything. Frontend lane; nothing here changes for it.

### T4 — Rate limits stay as the inner layer

Unchanged. They are what holds a real visitor, or a compromised frontend, to
a fair share. They are not a substitute for T1–T3 and T1–T3 do not replace
them.

## The allowlist

Inventoried from every router on 2026-10-10 (`Depends(require_…)`,
`Security(…)` and the webhook receiver). Exact method and template each —
`{param}` is one path segment — never a prefix, so a route added beside one
of these is not exempt by accident.

| Method | Path | Open to | Why |
| --- | --- | --- | --- |
| `POST` | `/api/pdax/webhooks/receive` | anyone | PDAX's servers deliver events here and cannot hold our token. Every delivery is authenticated by its HMAC signature (`PDAX_WEBHOOK_SECRET`) and the body is capped at 64 KiB. |
| `GET` | `/api/health` | anyone | The liveness probe re-served under `/api` for monitors. Says nothing `/health` does not. |
| `POST` | `/api/disputes/{dispute_id}/uphold`, `/reject` | operator key | `require_adjudicator`. Decided from an operator's terminal. |
| `POST` | `/api/stellar/reputation/{agent_id}/invalidate` | operator key | `require_operator_key`. Called directly by `scripts/uphold_dispute.py`. |
| `POST` | `/api/stellar/server/charge`, `/server/seal` | operator key | `require_api_key` / `require_seal_key`. Backend-signed contract calls. |
| various | the 24 keyed `/api/pdax/*` routes (trade, funding, withdrawals, ramps, balances, webhook registration, deep health) | operator key | The `secured` router's `require_api_key`. Money-moving and account-revealing, driven by operator tooling. |

**"Operator key" means the lock checks it too.** A keyed route passes the lock
only when the request carries `X-API-Key` equal to `API_KEY` (the same
constant-time `header_secret_matches` the routes use). Exempting those routes
outright would have left a gap: while `API_KEY` is empty, `require_api_key` is
a no-op, so the PDAX and charge routes would have stayed open to any direct
caller behind the lock's back. With the check, an empty `API_KEY` opens none
of them.

**Not on the list.** Seven reads take the operator key only as an optional
elevation — `GET /api/tasks/{task_id}`, `/artifact`, `/disputes`,
`GET /api/disputes/{dispute_id}`, `GET /api/trace/{task_id}` and `/stream`,
`GET /api/agents/{agent_id}/binding`. They are the console's own reads, served
to anyone with the task's read token, so they stay locked; an operator who
wants one goes through `orizons.xyz`, which forwards `X-API-Key` unchanged.

`tests/test_origin_lock.py` pins the keyed rows to the operations the OpenAPI
says take the operator key, less those seven: a newly keyed route, or a key
taken off one, fails the suite until this list says so.

## Rollout

1. **Token on both sides.** Confirm `FRONTEND_PROXY_TOKEN` is set, to the same
   value, on Render and on Vercel (Production and Preview). Without it on
   Vercel the frontend sends nothing and every request it makes counts as
   locked out.
2. **Deploy in `log`** (the default). Nothing is refused.
3. **Watch.** `/readiness` → `origin_lock.would_block_last_hour`, and the
   `origin lock would refuse …` WARNING lines in Render's log, which name the
   route and the client. Expected: scanners and stray direct calls. Not
   expected: steady traffic on routes the console uses (a proxy path that
   drops the token), or a script of ours. Fix each at the caller.
4. **Enforce.** Set `ORIGIN_LOCK_MODE=enforce` in the Render dashboard (it
   overrides `render.yaml`) and redeploy. The process refuses to boot if the
   token is missing, so a bad switch fails the deploy rather than the site.
5. **Docs dark.** Set `DOCS_ENABLED=false` in production so `/docs`, `/redoc`
   and `/openapi.json` stop advertising the routes. They sit outside `/api/`
   and the lock never covers them.

Back out by setting `ORIGIN_LOCK_MODE=log` (or `off`); no code change.

## Consequences

- Direct calls to `orizon-agents-be-stellar.onrender.com/api/*` are counted,
  then refused. Every legitimate caller uses `https://orizons.xyz/api/*`, or
  presents the operator key on a keyed route.
- **Our own scripts.** Those that default to `https://orizons.xyz`
  (`lifecycle`, `adoption_report`, `demo_preflight`'s API checks,
  `sow_metrics`' API checks, `verify_external_settlement`) are unaffected.
  `scripts/uphold_dispute.py` calls the invalidate route with the key and
  passes. `verify_registration.py --api-base` must be given `orizons.xyz`,
  not the Render host. `demo_preflight` and `sow_metrics` read `/readiness`
  from the Render host, which stays open; `sow_metrics` also reads
  `/openapi.json` there, which stops answering once `DOCS_ENABLED=false`.
- A refused request costs one header compare and no rate-limit budget; a
  flood of them costs one log line per route per minute and one counter
  increment each.
- `/readiness` gains `origin_lock` (additive). The counts are this process's,
  since its boot; Render's free tier restarts on wake, so they cover the
  current instance only.
- The frontend token now gates access, not only rate-limit identity. Rotating
  it means setting the new value on both sides; until both match, `enforce`
  refuses the frontend. Rotate in `log`.
- The allowlist is code. A new direct caller (a second webhook provider, a
  new operator route) needs an entry here and in `app/origin_lock.py`, and
  the drift test enforces that for keyed routes.
