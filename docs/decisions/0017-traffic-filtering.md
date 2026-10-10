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
