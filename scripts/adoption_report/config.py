"""Targets, exit codes, network constants and run configuration for the adoption report."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The only network this verifier reads. SOW §3.6 makes the sprint testnet-only,
# and a report that verified mainnet claims against testnet contracts (or the
# reverse) would be evidence of nothing. So the passphrase is checked against
# the RPC's own `getNetwork` AND Horizon's root document before any claim is read.
TESTNET_PASSPHRASE = "Test SDF Network ; September 2015"
TESTNET_RPC = "https://soroban-testnet.stellar.org"
TESTNET_HORIZON = "https://horizon-testnet.stellar.org"

EXPLORER = "https://stellar.expert/explorer/testnet"
EXPLORER_ACCOUNT = EXPLORER + "/account/{}"
EXPLORER_TX = EXPLORER + "/tx/{}"
EXPLORER_CONTRACT = EXPLORER + "/contract/{}"

# Where Lane P commits the register of wallets the Blocksmiths control.
DEFAULT_TEAM_REGISTER = Path("app/data/team_wallets.json")

ADOPTION_PATH = "/api/ecosystem/adoption"

# The endpoint answers 202 {"status": "computing"} with Retry-After while the
# first report since the service booted is computed — a settlement scan per
# external agent, minutes on the live registry (D-091). The verifier waits for
# it, as told, but never longer than this in total, nor more than the bounds
# below between two asks.
PENDING_MAX_WAIT_SECONDS = 600.0
PENDING_MIN_DELAY_SECONDS = 1.0
PENDING_MAX_DELAY_SECONDS = 60.0
PENDING_DEFAULT_DELAY_SECONDS = 30.0

# SOW §6.3, as story 5.02 (BLO-36) states it. The verifier holds its own copy
# on purpose: an API that lowered a target to make itself MET is one of the
# claims being checked.
TARGETS: dict[str, int] = {
    "external_agents": 2,
    "unique_operator_wallets": 2,
    "settled_external_workflows": 3,
}
TARGET_LABELS: dict[str, str] = {
    "external_agents": "Externally operated agents",
    "unique_operator_wallets": "Unique operator wallets",
    "settled_external_workflows": "Workflows routed to external agents and settled",
}

EXCLUSION_REASONS = frozenset({"team_wallet", "platform_key"})

# Stellar amounts carry seven decimals; the escrow's i128 amounts are stroops.
STROOPS_PER_UNIT = 10_000_000

# Exit codes, so a wrapper (the 5.05 evidence index) can tell a refusal from a
# forged claim from a miscount without parsing prose. 1 is an unhandled
# traceback and 2 is argparse, so neither is reused.
EXIT_OK = 0
EXIT_REFUSED = 3  # preconditions not met: wrong network, bad flags, unreadable register
EXIT_API_UNREADABLE = 4  # the endpoint did not answer, or answered outside the frozen shape
EXIT_CLAIM_FAILED = 5  # the chain does not show what the API claims
EXIT_COUNT_MISMATCH = 6  # every claim held, but the API's totals or MET flags disagree with the recount
EXIT_TARGET_NOT_MET = 7  # only with --require-met: everything verified, and a target is NOT MET
EXIT_CHAIN_UNREADABLE = 8  # no claim was contradicted, but a chain read failed, so not every claim is verified


@dataclass(frozen=True)
class RunConfig:
    """Everything one invocation was asked to verify. Holds no secret: none is needed."""

    api: str
    rpc_url: str
    horizon_url: str
    escrow: str
    registry: str
    team_register: Path
    out_dir: Path | None
    require_met: bool = False


def normalize_api_base(raw: str) -> str:
    """Strip trailing slashes and one trailing `/api` (the frontend's `lib/api-base.mjs` rule).

    The verifier appends `/api/...` itself, so `https://orizons.xyz/api/` and
    `https://orizons.xyz` must name the same base.
    """
    base = raw.strip().rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"--api must be an absolute http(s) URL, got {raw!r}")
    return base


def usdc_to_stroops(amount: float) -> int:
    """0.01 -> 100000, rounded to the nearest stroop as the backend does."""
    return round(amount * STROOPS_PER_UNIT)
