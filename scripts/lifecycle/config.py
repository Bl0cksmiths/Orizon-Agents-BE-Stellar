"""Stages, exit codes and run configuration for the lifecycle harness."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The only network this harness will sign on. SOW §3.6 makes the whole sprint
# testnet-only, and the harness signs with a buyer key and spends the settler's
# balance through the adjudicator route: pointed at mainnet by a typo, that is
# real money. So the passphrase is checked twice, against the API's own
# `/api/stellar/network` and against the RPC's `getNetwork`, before anything is
# built or signed — in a dry run too.
TESTNET_PASSPHRASE = "Test SDF Network ; September 2015"
EXPLORER_TX = "https://stellar.expert/explorer/testnet/tx/{}"
TESTNET_HORIZON = "https://horizon-testnet.stellar.org"

# The authorization's label. v1: the console authorizes a whole plan as
# `orizon_batch` (execution-plan.tsx on FE main). v2: the frozen interface
# (contracts repo docs/escrow-v2-interface.md, amended cfa2ca4 "bound the
# authorization label to the plan id") makes the label the PLAN ID, and the
# backend refuses to execute a plan against an authorization labelled for
# another (finding S2). `authorize_label` picks by the escrow's version.
BATCH_AGENT_ID = "orizon_batch"
# `plan.total_usdc > 0 ? plan.total_usdc : MIN_CAP` (execution-plan.tsx).
MIN_AUTHORIZE_AMOUNT = 0.001
# v1: `ttl_seconds: 600` (FE main). v2: 1800 (FE lib/escrow.ts
# AUTHORIZE_TTL_SECONDS on feat/5.01-escrow-v2-ui) — a v2 execute is refused
# when the authorization would expire before a worst-case run could settle.
AUTHORIZE_TTL_SECONDS = 600
AUTHORIZE_TTL_SECONDS_V2 = 1800


def authorize_label(escrow_version: int, plan_id: str) -> str:
    return plan_id if escrow_version >= 2 else BATCH_AGENT_ID


def authorize_ttl(escrow_version: int) -> int:
    return AUTHORIZE_TTL_SECONDS_V2 if escrow_version >= 2 else AUTHORIZE_TTL_SECONDS


# The nine stages, in order. The name is what `--until` takes and what every
# evidence row is filed under.
STAGES: tuple[str, ...] = (
    "decompose",
    "authorize",
    "execute",
    "poll",
    "verify",
    "dispute",
    "uphold",
    "refund",
    "reputation",
)

# Exit codes, so a wrapper can tell a refusal from a failure from an unknown
# outcome without parsing prose. 1 is an unhandled traceback and 2 is argparse,
# so neither is reused.
EXIT_OK = 0
EXIT_REFUSED = 3  # preconditions not met: wrong network, missing env, bad args
EXIT_PLAN_MISSING_AGENT = 4  # the plan does not route to every --agent; nothing signed
EXIT_STAGE_FAILED = 5  # a definitive failure: the server or the ledger said no
EXIT_UNKNOWN_OUTCOME = 6  # sent, outcome unknown: state read back, run stopped
EXIT_VERIFY_FAILED = 7  # the chain does not show what the API says happened
EXIT_TIMED_OUT = 8  # waited the full budget for a state that never came
EXIT_RESUME_CONFLICT = 9  # the evidence dir holds a run this invocation would clobber


@dataclass(frozen=True)
class Budgets:
    """How long each wait may take, in seconds. Injected so tests run in zero time."""

    warmup: float = 150.0  # Render's free tier boots in 30-60 s; give it room
    task: float = 900.0  # a workflow with several external dispatches
    poll_interval: float = 4.0  # lib/api.ts TRACE_POLL_MS
    tx_observe: float = 60.0  # a submitted tx reaching getTransaction
    refund: float = 300.0  # credit + dispute rating after the uphold returns
    refund_interval: float = 5.0  # use-dispute-panel.ts CREDIT_POLL_MS


@dataclass(frozen=True)
class RunConfig:
    """Everything one invocation was asked to do. Holds no secret: the two
    credentials are named by the environment variable that carries them."""

    api: str
    # The first --agent: the one the run is named after, whose reputation is
    # snapshotted, and the first choice to dispute.
    agent: str
    intent: str | None
    buyer_secret_env: str
    adjudicator_key_env: str | None
    evidence_dir: Path
    dry_run: bool = False
    until: str = STAGES[-1]
    from_task: str | None = None
    from_dispute: str | None = None
    dispute_reason: str = "Harness 5.01: the delivered output did not meet the plan's stated step."
    rpc_url: str | None = None
    horizon_url: str = TESTNET_HORIZON
    # Every --agent, in the order given, `agent` first. Empty means `agent`
    # alone. The plan must route to all of them (order does not matter).
    agents: tuple[str, ...] = ()

    @property
    def all_agents(self) -> tuple[str, ...]:
        return self.agents or (self.agent,)

    @property
    def multi_agent(self) -> bool:
        return len(self.all_agents) > 1


def normalize_api_base(raw: str) -> str:
    """`lib/api-base.mjs`'s rule: strip trailing slashes and one trailing `/api`.

    The harness appends `/api/...` itself, so `https://orizons.xyz/api/` and
    `https://orizons.xyz` must name the same base — otherwise every call is a
    404 that still carries the backend's error envelope, the outage the
    frontend's own normalizer was written after.
    """
    base = raw.strip().rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"--api must be an absolute http(s) URL, got {raw!r}")
    return base


def stage_index(name: str) -> int:
    return STAGES.index(name)
