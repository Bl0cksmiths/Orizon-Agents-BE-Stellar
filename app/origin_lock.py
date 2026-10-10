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

import re
from dataclasses import dataclass, field

from .config import settings
from .security import header_secret_matches, is_frontend

# Only what the frontend proxies is locked. Everything else on this host is a
# probe, the root ping or the docs, none of which the frontend forwards.
API_PREFIX = "/api/"

_OPERATOR_KEY_HEADER = b"x-api-key"


def template_regex(template: str) -> re.Pattern[str]:
    """A route template as a regex to full-match a path: each `{param}` is exactly one segment.

    The same reading Starlette gives a plain `{param}`, so an exemption covers
    precisely the paths its route serves — `/api/disputes/{dispute_id}/uphold`
    admits `/api/disputes/d_1/uphold` and never `/api/disputes/d/1/uphold`.
    """
    parts = re.split(r"(\{[^/{}]+\})", template)
    return re.compile("".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts))


@dataclass(frozen=True)
class Exemption:
    """One route a caller may reach without our frontend: a method and an exact template.

    `keyed` routes are open to a direct caller only while it presents the
    operator's X-API-Key: the route checks that key itself, and requiring it
    here too keeps a deployment whose API_KEY is empty — where
    `require_api_key` waves everyone through — from leaving those routes open
    to the world behind the lock's back.
    """

    method: str
    template: str
    keyed: bool
    pattern: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pattern", template_regex(self.template))

    def admits(self, scope: dict) -> bool:
        if scope.get("method") != self.method or self.pattern.fullmatch(str(scope.get("path", ""))) is None:
            return False
        if not self.keyed:
            return True
        supplied = dict(scope.get("headers") or []).get(_OPERATOR_KEY_HEADER)
        return header_secret_matches(
            supplied.decode("latin-1") if supplied is not None else None,
            settings.api_key,
        )


def _open(method: str, template: str) -> Exemption:
    return Exemption(method, template, keyed=False)


def _keyed(method: str, template: str) -> Exemption:
    return Exemption(method, template, keyed=True)


# Everyone who may reach /api/* without our frontend, and why. Exact templates,
# never prefixes, so nothing new is exempt by being added next to one of these.
# tests/test_origin_lock.py pins the keyed list to the operations the OpenAPI
# says are behind the operator key, so a newly keyed route — or a key removed —
# fails a test instead of drifting.
EXEMPTIONS: tuple[Exemption, ...] = (
    # PDAX's own servers deliver deposit and withdrawal events here. They cannot
    # hold our frontend's token; the receiver authenticates every delivery by
    # its HMAC signature (PDAX_WEBHOOK_SECRET) instead, and caps the body.
    _open("POST", "/api/pdax/webhooks/receive"),
    # The liveness probe re-served under /api for monitors polling through the
    # proxy. It says nothing /health does not already say to anyone.
    _open("GET", "/api/health"),
    # Dispute adjudication — `require_adjudicator`. An operator decides these
    # from a terminal, not from the console.
    _keyed("POST", "/api/disputes/{dispute_id}/uphold"),
    _keyed("POST", "/api/disputes/{dispute_id}/reject"),
    # Reputation cache invalidation — `require_operator_key`. Called directly by
    # scripts/uphold_dispute.py after a rating lands.
    _keyed("POST", "/api/stellar/reputation/{agent_id}/invalidate"),
    # Backend-signed contract calls — `require_api_key` / `require_seal_key`.
    _keyed("POST", "/api/stellar/server/charge"),
    _keyed("POST", "/api/stellar/server/seal"),
    # PDAX trading, funding and ramps — the `secured` router's `require_api_key`.
    # Money-moving and account-revealing, driven by operator tooling.
    _keyed("GET", "/api/pdax/health/deep"),
    _keyed("GET", "/api/pdax/trade/price"),
    _keyed("GET", "/api/pdax/trade/price/v2"),
    _keyed("POST", "/api/pdax/trade/quote"),
    _keyed("POST", "/api/pdax/trade/quote/v2"),
    _keyed("POST", "/api/pdax/trade/order"),
    _keyed("GET", "/api/pdax/trade/orders/{order_id}"),
    _keyed("GET", "/api/pdax/trade/orders"),
    _keyed("GET", "/api/pdax/crypto/deposit"),
    _keyed("POST", "/api/pdax/fiat/deposit"),
    _keyed("POST", "/api/pdax/fiat/withdraw"),
    _keyed("POST", "/api/pdax/fiat/user-info-upload"),
    _keyed("POST", "/api/pdax/crypto/withdraw"),
    _keyed("GET", "/api/pdax/fiat/transactions"),
    _keyed("GET", "/api/pdax/crypto/transactions"),
    _keyed("GET", "/api/pdax/balances"),
    _keyed("POST", "/api/pdax/webhooks/register"),
    _keyed("POST", "/api/pdax/ramp/estimate"),
    _keyed("POST", "/api/pdax/ramp/funding-quote"),
    _keyed("POST", "/api/pdax/ramp/onramp"),
    _keyed("POST", "/api/pdax/ramp/offramp"),
    _keyed("GET", "/api/pdax/ramp"),
    _keyed("GET", "/api/pdax/ramp/{ramp_id}"),
    _keyed("POST", "/api/pdax/ramp/{ramp_id}/reconcile"),
)


def exempt(scope: dict) -> bool:
    """Whether an allowlisted caller is making this request (see EXEMPTIONS)."""
    return any(exemption.admits(scope) for exemption in EXEMPTIONS)


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
    return not is_frontend(scope) and not exempt(scope)
