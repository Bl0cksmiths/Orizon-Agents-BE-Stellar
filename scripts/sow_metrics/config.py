"""Exit codes, network constants, the SOW §6.3 rows and run configuration for the metrics generator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The only network measured. SOW §3.6 makes the sprint testnet-only, and a
# metric counted on mainnet (or on a testnet the API does not use) would be
# evidence of nothing. The passphrase is checked against the RPC's
# `getNetwork`, Horizon's root document AND the API's `/api/stellar/network`
# before a single metric is read; any disagreement is a refusal.
TESTNET_PASSPHRASE = "Test SDF Network ; September 2015"
TESTNET_RPC = "https://soroban-testnet.stellar.org"
TESTNET_HORIZON = "https://horizon-testnet.stellar.org"

# orizons.xyz proxies only `/api/*` to the backend, so `/readiness` and
# `/openapi.json` are read from the backend's own host.
DEFAULT_API = "https://orizons.xyz/api"
DEFAULT_FRONTEND = "https://orizons.xyz"
DEFAULT_BACKEND = "https://orizon-agents-be-stellar.onrender.com"
DEFAULT_GITHUB_API = "https://api.github.com"
DEFAULT_TEAM_REGISTER = Path("app/data/team_wallets.json")

# Escrows measured besides the one the live API names. The v1 escrow is here
# so its history keeps being counted (and excluded, with reasons) after the
# deployment moves to v2: a settlement does not stop existing because the
# backend stopped pointing at its contract. `--escrow` adds more.
KNOWN_ESCROWS: tuple[str, ...] = ("CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI",)

# The 30-day sprint began on this day (UTC). A settlement or refund before it
# is history, not sprint evidence.
SPRINT_START = datetime(2026, 9, 7, tzinfo=UTC)

# SOW §6.1: the repositories released for the sprint (the ALGOREX-PH URLs it
# names redirect to Bl0cksmiths), plus the copyable example agent.
REPOSITORIES: tuple[tuple[str, str], ...] = (
    ("Bl0cksmiths/Orizon-Agents-FE-Stellar", "frontend"),
    ("Bl0cksmiths/Orizon-Agents-BE-Stellar", "backend"),
    ("Bl0cksmiths/Orizon-Agents-Smart-Contract-Stellar", "smart contracts"),
    ("Bl0cksmiths/Orizon-Agents-Example-Agent-Stellar", "example agent"),
)

# The pages and routes the milestones are checked against.
REGISTER_PAGE = "/app/register"
GUIDE_PAGE = "/guide/list-your-agent"
DEMO_PAGE = "/demo"
REGISTER_ROUTE = "POST /api/stellar/build/register-agent"
DISPUTE_ROUTES: tuple[str, ...] = (
    "POST /api/disputes",
    "GET /api/disputes/{dispute_id}",
    "POST /api/disputes/{dispute_id}/uphold",
    "POST /api/disputes/{dispute_id}/reject",
)
# The demo video must run 3 to 5 minutes (the frontend's lib/demo/validate.mjs).
DEMO_MIN_SECONDS = 180
DEMO_MAX_SECONDS = 300

# How far a dispute rating's step index is searched when tracing its derived
# job id back to the settled job (app/services/dispute_rating.py packs it in
# two bytes; no plan comes near this many steps).
MAX_TRACED_STEPS = 256

STROOPS_PER_UNIT = 10_000_000

EXPLORER = "https://stellar.expert/explorer/testnet"
EXPLORER_ACCOUNT = EXPLORER + "/account/{}"
EXPLORER_TX = EXPLORER + "/tx/{}"
EXPLORER_CONTRACT = EXPLORER + "/contract/{}"


@dataclass(frozen=True)
class SowRow:
    """One SOW §6.3 row, verbatim."""

    id: str
    category: str
    metric: str
    target: str
    threshold: int | None  # the count to reach; None for a Yes/No milestone


# SOW v4 §6.3, character for character. The frozen block repeats these, and
# the frontend's evidence index is pasted from the block.
SOW_ROWS: tuple[SowRow, ...] = (
    SowRow("m01", "Adoption targets", "Externally-operated agents registered on Testnet", "≥ 2", 2),
    SowRow("m02", "Adoption targets", "Unique external operator wallet addresses", "≥ 2", 2),
    SowRow("m03", "Transaction targets", "Workflows routed to external agents & settled on Testnet", "≥ 3", 3),
    SowRow("m04", "Transaction targets", "On-chain USDC settlements (charges) recorded", "≥ 3", 3),
    SowRow("m05", "Transaction targets", "Dispute → partial-refund settlements", "≥ 1", 1),
    SowRow("m06", "Technical milestones", "Permissionless AgentRegistry.register flow live on the dApp", "Yes", None),
    SowRow(
        "m07", "Technical milestones", "Reputation-gated routing (reads avg_bps, applies a floor) live", "Yes", None
    ),
    SowRow("m08", "Technical milestones", "Automated dispute window + partial-credit refund live", "Yes", None),
    SowRow(
        "m09",
        "Technical milestones",
        'Public "List your agent on Orizon" integration guide published',
        "Yes",
        None,
    ),
    SowRow("m10", "Technical milestones", "3–5 min demo video published", "Yes", None),
    SowRow("m11", "Technical milestones", "All source code released under MIT License", "Yes", None),
)

# Exit codes, so a wrapper can tell a refusal from a read failure without
# parsing prose. 1 is an unhandled traceback and 2 is argparse. Met or not met
# is NOT an exit code: a measured miss is a successful measurement.
EXIT_MEASURED = 0  # every metric was measured, whatever it came to
EXIT_REFUSED = 3  # wrong network, bad flags, an unreadable team register: nothing was measured
EXIT_UNREADABLE = 4  # a read failed, so at least one metric is reported "Not measured" (never 0, never met)

BLOCK_NAME = "sow-metrics.block.json"
MARKDOWN_NAME = "sow-metrics.md"
RAW_NAME = "sow-metrics.raw.json"


@dataclass(frozen=True)
class RunConfig:
    """Everything one invocation measures against. Holds no secret: none is needed."""

    api: str
    frontend: str
    backend: str
    rpc_url: str
    horizon_url: str
    github_api: str
    team_register: Path
    extra_escrows: tuple[str, ...]
    out_dir: Path | None


def normalize_base(raw: str, flag: str, *, strip_api: bool = False) -> str:
    """An absolute http(s) base without trailing slashes (and, for `--api`, without one trailing `/api`)."""
    base = raw.strip().rstrip("/")
    if strip_api and base.endswith("/api"):
        base = base[: -len("/api")]
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"{flag} must be an absolute http(s) URL, got {raw!r}")
    return base
