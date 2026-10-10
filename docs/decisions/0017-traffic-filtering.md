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
