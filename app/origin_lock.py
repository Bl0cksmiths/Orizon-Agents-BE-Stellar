"""Origin lock — /api/* answers our frontend, and nobody who walks around it (ADR 0017).

The public entry point is https://orizons.xyz: Vercel's firewall sits in front
of it, and its Next.js middleware forwards every `/api/*` request here with
X-Frontend-Proxy-Token. Render's own hostname is just as reachable, though,
and a caller who uses it skips the firewall entirely. This layer closes that
door: a request under `/api/` that does not carry the token (checked by
`security.is_frontend`, in constant time) is one the lock would refuse.

What happens to it is ORIGIN_LOCK_MODE's call:

  * `off` — nothing; every request is served as before.
  * `log` — served, but counted and logged, so /readiness and the log show
    who would be locked out before anyone is. The default, and the rollout's
    first step.
  * `enforce` — answered 403 `origin_forbidden` in the unified error envelope.
    `config` refuses to boot this mode without a token, since then nothing
    could pass.

Never locked: anything outside `/api/` (the probes, the root ping, the docs,
which DOCS_ENABLED governs), CORS preflights, and the allowlist below.
"""

from __future__ import annotations

from .security import is_frontend

# Only what the frontend proxies is locked. Everything else on this host is a
# probe, the root ping or the docs, none of which the frontend forwards.
API_PREFIX = "/api/"


def locked_out(scope: dict) -> bool:
    """Whether the lock refuses this request — in `enforce`; `log` only reports it.

    Pure over the scope and the current settings: it reads no body and makes
    no call, so a request it refuses costs a header lookup and one
    constant-time comparison.
    """
    if scope.get("type") != "http":
        return False
    if not str(scope.get("path", "")).startswith(API_PREFIX):
        return False
    # A preflight carries no credentials by design — the browser sends it on
    # its own, before the real request — so it can never present the token.
    # CORSMiddleware answers it outside this layer anyway; this keeps a bare
    # OPTIONS from being counted as someone locked out.
    if scope.get("method") == "OPTIONS":
        return False
    return not is_frontend(scope)
