"""Exit codes, network constants, thresholds and run configuration for the demo pre-flight."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# The only network the demo is recorded on. SOW §3.6 makes the sprint
# testnet-only, and a video whose hashes resolve on mainnet (or not at all)
# would be evidence of nothing. The passphrase is checked against the RPC's
# `getNetwork`, Horizon's root document AND the API's `/api/stellar/network`
# before a single check runs; any disagreement is a refusal, not a FAIL.
TESTNET_PASSPHRASE = "Test SDF Network ; September 2015"
TESTNET_RPC = "https://soroban-testnet.stellar.org"
TESTNET_HORIZON = "https://horizon-testnet.stellar.org"

# orizons.xyz proxies only `/api/*` to the backend, so the root-level
# `/readiness` probe is read from the backend's own host. `--backend` overrides it.
DEFAULT_API = "https://orizons.xyz/api"
DEFAULT_FRONTEND = "https://orizons.xyz"
DEFAULT_BACKEND = "https://orizon-agents-be-stellar.onrender.com"
DEFAULT_TEAM_REGISTER = Path("app/data/team_wallets.json")

# Every page the recording visits, and none that needs a login to answer.
# `/app/trace` carries the receipt and the dispute (S07-S09), and `/demo` is
# where the published video and its evidence sheet are read.
FRONTEND_PAGES: tuple[str, ...] = (
    "/app/register",
    "/app/bind",
    "/app/operator",
    "/app/orchestrator",
    "/app/agents",
    "/app/trace",
    "/app/ecosystem",
    "/guide/list-your-agent",
    "/demo",
)

# Render's free tier boots in 30-60 s, and a sleeping one can take longer.
# The health probe is asked again until this budget is spent.
WARMUP_BUDGET_SECONDS = 120.0

# A backend that has just woken reads every agent's reputation as `degraded`
# (the prior, served because the ledger read has not answered yet). When EVERY
# registered agent reads degraded, the batch is read again, this many times
# in all and this far apart, before anything is concluded from it.
REPUTATION_READ_ATTEMPTS = 3
REPUTATION_REREAD_SECONDS = 5.0

# The backend's own defaults, restated. `MAX_REFUND_USDC` is not on any public
# route, so the pre-flight takes it as a flag and says which value it assumed;
# the Render dashboard overrides render.yaml, so only the operator knows the
# value in force.
DEFAULT_MAX_REFUND = 1.0  # app/config.py `max_refund_usdc`
# What the recorded plan may cost the buyer at most. The plan's cap is what
# the buyer authorizes, so the buyer's spendable balance has to cover it.
DEFAULT_CAP = 1.0

# Fee headroom, in XLM, on top of each wallet's spend. A Soroban invocation on
# testnet costs a few hundredths of an XLM in resource fees; these leave room
# for a retake. The settler pays for the settle, the seal, every rating, the
# refund transfer and the dispute rating of each take.
BUYER_FEE_ALLOWANCE = 0.5
OPERATOR_FEE_ALLOWANCE = 0.5
SETTLER_FEE_ALLOWANCE = 2.0
# Stellar's base reserve, per entry: an account holds (2 + subentries) of them.
BASE_RESERVE = 0.5

STROOPS_PER_UNIT = 10_000_000

EXPLORER = "https://stellar.expert/explorer/testnet"
EXPLORER_ACCOUNT = EXPLORER + "/account/{}"
EXPLORER_CONTRACT = EXPLORER + "/contract/{}"

# Exit codes, so a wrapper can tell a refusal from a NO-GO without parsing
# prose. 1 is an unhandled traceback and 2 is argparse, so neither is reused.
EXIT_GO = 0
EXIT_REFUSED = 3  # wrong network, bad flags, an unreadable team register: no check was judged
EXIT_NO_GO = 4  # a required check FAILED
EXIT_INCOMPLETE = 5  # nothing FAILED, but a required check was SKIPPED — and SKIPPED is never a pass

MARKDOWN_NAME = "demo-preflight.md"
JSON_NAME = "demo-preflight.json"


@dataclass(frozen=True)
class RunConfig:
    """Everything one invocation was asked to check. Holds no secret: none is needed."""

    api: str
    frontend: str
    backend: str
    rpc_url: str
    horizon_url: str
    team_register: Path
    buyer: str | None
    operator: str | None
    cap: float
    max_refund: float
    allow_team_operator: bool
    decompose_intent: str | None
    out_dir: Path | None
    operator_endpoint: str | None = None  # the origin only: a bound URL's query can carry a shared secret


def normalize_base(raw: str, flag: str, *, strip_api: bool = False) -> str:
    """An absolute http(s) base without trailing slashes (and, for `--api`, without one trailing `/api`).

    `https://orizons.xyz/api/` and `https://orizons.xyz` name the same API:
    the pre-flight appends `/api/...` itself (`lib/api-base.mjs`'s rule).
    """
    base = raw.strip().rstrip("/")
    if strip_api and base.endswith("/api"):
        base = base[: -len("/api")]
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"{flag} must be an absolute http(s) URL, got {raw!r}")
    return base


def endpoint_origin(raw: str) -> str:
    """The scheme and host of an agent endpoint, without its path, query or fragment.

    The reference agent answers its health check on any GET path, and a bound
    endpoint may carry a shared secret in its query string, which must never
    reach the terminal or the report. So only the origin is kept, and `/` is
    asked there.
    """
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("--operator-endpoint must be an absolute http(s) URL (not shown: it may carry a secret)")
    return f"{parts.scheme}://{parts.netloc}"


def to_units(stroops: int) -> float:
    return stroops / STROOPS_PER_UNIT
