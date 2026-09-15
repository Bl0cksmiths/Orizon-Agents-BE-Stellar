"""
Reputation service — Bayesian-smoothed, evidence-weighted agent reputation.

Division of labour (mirrors ERC-8004: raw evidence on-chain, aggregation off):

  - The ReputationLedger contract stores decayed, value-weighted rating
    evidence per agent (`rep_state` → sum_w / weight / count / disputed).
  - This service applies the Bayesian prior (Jøsang-style beta smoothing),
    derives a conservative Wilson-style lower bound for the routing floor,
    and computes the synthetic per-step rating the settler submits after a
    settled workflow.

Score semantics: ratings are 0–100; every *_bps value here is basis points
of that scale (0..10_000). Weights are USDC in stroops (7 decimals) — a
rating earned on a 0.054 USDC step carries less evidence than one earned on
an 0.180 USDC step, so reputation is weighted by what a step was worth
rather than by a count of clicks.

"Worth" is the step's QUOTED price, not money that changed hands. The
settler passes `step.est_price_usdc`, and a failed step is never billed
(ADR 0005 D1) yet is rated all the same — so the weight measures what was at
stake, which is as true of a step that failed as of one that delivered. This
is deliberately NOT "a record of settled economic history": that reading
would make every negative rating weightless, since non-delivery settles
nothing.

Cold start: with no on-chain evidence the smoothed score IS the prior
(default 7000 = 3.5/5) and the lower bound still clears the default floor —
permissionless newcomers are routable, while a few heavily-weighted bad
ratings sink an agent below the floor quickly. Failures never fabricate
evidence: if the chain is unreachable the caller gets the prior, marked
`source="prior"` and `degraded=True`.

Degradation policy (deliberate, not accidental) — when the ledger cannot be
read, reads fall back to the prior and the routing floor therefore fails
OPEN under the shipped config: a prior-only agent clears the floor, so
low-reputation agents become routable for the duration of the outage. That
is kept, for three reasons:

  - It is the same position the system takes on any agent it knows nothing
    about. An unreadable ledger genuinely means "reputation unknown", and
    the product's answer to unknown reputation is "routable" (cold start).
  - Failing closed would not fail closed. Every agent would drop below the
    floor at once, tripping the orchestrator's _MIN_ROUTABLE_AGENTS backstop,
    which then picks a top-N by identical prior scores — the same agents get
    hired, with less defined semantics.
  - The window is bounded by the read TTL and by the batch timeout.

What was actually broken is that none of this was visible: the fallbacks
logged at DEBUG under an INFO root logger, i.e. not at all. Every fallback
now marks the RepInfo `degraded` and emits one WARNING per batch that names
the agents and states which way the floor is failing (`_log_degraded`).
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Literal

from pydantic import BaseModel

from ..config import settings

logger = logging.getLogger(__name__)

STROOPS_PER_USDC = 10_000_000
# One-sided ~84% confidence. Deliberately gentle: paired with the prior mass
# it lets a prior-only newcomer clear the floor, while real negative evidence
# still drags the bound down fast.
WILSON_Z = 1.0

# Mirrors of the deployed ReputationLedger v2 on-chain constants
# (contract/reputation-ledger/src/lib.rs) — decay happens on-chain; these are
# surfaced read-only so the API can describe the full scoring pipeline.
EPOCH_SECONDS = 604_800  # one decay epoch = 1 week
DECAY_BPS_PER_EPOCH = 9_250  # evidence retains 92.5% per epoch (~9-week half-life)
MAX_DECAY_EPOCHS = 96  # beyond this, stale evidence is fully forgotten

# Cap on agent ids named in a degradation warning — the batch is the whole
# registry, and the line has to stay readable in Render's log viewer.
_DEGRADED_LOG_AGENT_LIMIT = 12

RepSource = Literal["onchain", "prior"]


class RepInfo(BaseModel):
    """Aggregated reputation for one agent, ready for routing and display."""

    agent_id: str
    smoothed_bps: int  # prior-smoothed mean, 0..10_000
    lower_bound_bps: int  # conservative bound used for the routing floor
    avg_bps: int  # unsmoothed decayed on-chain mean (0 = no evidence)
    count: int  # lifetime rating count
    weight: int  # decayed evidence mass, stroops
    disputed: int  # lifetime dispute count
    dispute_rate_bps: int  # disputed / count, in bps
    source: RepSource
    # True when this prior is a FALLBACK for an unreadable ledger rather than
    # a genuine cold start. `source` alone conflates the two (both are
    # "prior"), which is precisely what made the routing floor's fail-open
    # invisible to callers. Additive with a safe default — the routers'
    # mirror models drop unknown keys, so no client contract changes.
    degraded: bool = False


def prior_weight_stroops() -> int:
    return round(settings.reputation_prior_weight_usdc * STROOPS_PER_USDC)


def smoothed_bps(sum_w: int, weight: int) -> int:
    """Bayesian smoothed mean: prior mass pulls sparse evidence to the prior."""
    pw = prior_weight_stroops()
    den = pw + weight
    if den <= 0:
        return settings.reputation_prior_bps
    num = pw * settings.reputation_prior_bps + sum_w
    return max(0, min(10_000, num // den))


def lower_bound_bps(mean_bps: int, weight: int) -> int:
    """Wilson-style lower bound on the smoothed mean.

    Effective sample size counts prior mass plus on-chain evidence in units
    of one-USDC jobs, so confidence grows with settled value, not raw count.
    """
    n = (prior_weight_stroops() + max(weight, 0)) / STROOPS_PER_USDC
    if n <= 0:
        return 0
    p = min(max(mean_bps / 10_000, 0.0), 1.0)
    lb = p - WILSON_Z * math.sqrt(p * (1.0 - p) / n)
    return max(0, min(10_000, round(lb * 10_000)))


def prior_clears_floor() -> bool:
    """Whether a prior-only agent clears the routing floor under the current
    config — i.e. whether `passes_floor` fails OPEN when the ledger is
    unreadable and every agent degrades to the prior.

    True with the shipped defaults (prior lower bound 5677 bps vs a 5500 bps
    floor). Read at log time rather than hard-coded, so an operator who
    raises REPUTATION_FLOOR_BPS above the prior bound gets a floor that fails
    closed AND a warning line that says so, instead of either policy being a
    silent property of the arithmetic. See the module docstring.
    """
    return lower_bound_bps(settings.reputation_prior_bps, 0) >= settings.reputation_floor_bps


def passes_floor(info: RepInfo | None) -> bool:
    """Routing-floor check on the conservative lower bound.

    Degraded (prior-fallback) infos are NOT special-cased: they are judged by
    the same arithmetic as a cold-start agent, so the configured floor stays
    the single authority on what is routable. With the default config that
    means an outage fails open — deliberately, and now loudly logged by
    `_log_degraded`; see the module docstring for why.
    """
    if info is None:
        return True
    return info.lower_bound_bps >= settings.reputation_floor_bps


def rating_weight_stroops(step_price_usdc: float) -> int:
    """Evidence weight of one rating: the step's QUOTED price, capped.

    Not its settled value. The settler passes `step.est_price_usdc` — what the
    step was quoted at — and every failure path skips the one billing site
    (ADR 0005 D1), so the 20/100 a non-delivery earns is weighted by money
    that was never charged. That is the intent, not an oversight: the weight
    says how much was at stake on the step, and a step that promised 0.180
    USDC of work and delivered none put 0.180 USDC at stake.

    The 1-stroop floor keeps a zero-priced step from carrying literally no
    evidence; `registry_sync` refuses an on-chain price low enough for that
    floor to be an exploit (ADR 0005 D4).
    """
    capped = min(max(step_price_usdc, 0.0), settings.reputation_max_rating_weight_usdc)
    return max(1, round(capped * STROOPS_PER_USDC))


def synthetic_rating(
    step_output: dict[str, Any] | None,
    step_price_usdc: float,
) -> tuple[int, int]:
    """Derive the settler's synthetic rating for one settled step.

    Returns (rating_0_to_100, weight_stroops). The rating is built from
    verifiable workflow signals (did the worker produce output, did it ship
    an artifact, did the critic find violations) — validation-gated
    reputation rather than opinion. Baked kit artifacts are deterministic
    and pre-validated by design, so they earn a fixed high score.
    """
    weight = rating_weight_stroops(step_price_usdc)

    if not step_output:
        # Timed out / raised — settled money for no delivered work.
        return 20, weight

    if step_output.get("source") == "baked":
        return 95, weight

    rating = 70
    if step_output.get("artifact"):
        rating += 15
    violations = step_output.get("critic_violations")
    if violations is None:
        violations = step_output.get("validator_violations")
    if isinstance(violations, list):
        rating += 10 if not violations else -3 * min(len(violations), 10)
    return max(0, min(100, rating)), weight


def _prior_info(agent_id: str, degraded: bool = False) -> RepInfo:
    prior = settings.reputation_prior_bps
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=prior,
        lower_bound_bps=lower_bound_bps(prior, 0),
        avg_bps=0,
        count=0,
        weight=0,
        disputed=0,
        dispute_rate_bps=0,
        source="prior",
        degraded=degraded,
    )


def _info_from_state(agent_id: str, state: dict[str, Any]) -> RepInfo:
    sum_w = int(state.get("sum_w", 0))
    weight = int(state.get("weight", 0))
    count = int(state.get("count", 0))
    disputed = int(state.get("disputed", 0))
    if count == 0 and weight == 0:
        # A readable ledger with no evidence IS the prior — report it as such
        # so clients can distinguish "rated on-chain" from "not yet rated".
        return _prior_info(agent_id)
    smoothed = smoothed_bps(sum_w, weight)
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed,
        lower_bound_bps=lower_bound_bps(smoothed, weight),
        avg_bps=(sum_w // weight) if weight > 0 else 0,
        count=count,
        weight=weight,
        disputed=disputed,
        dispute_rate_bps=(disputed * 10_000 // count) if count > 0 else 0,
        source="onchain",
    )


def _describe(e: BaseException) -> str:
    """Compact "Type: message" description, bare type when there is no
    message (asyncio.TimeoutError carries none)."""
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _log_degraded(agent_ids: list[str], total: int, reason: str) -> None:
    """Emit ONE warning for a set of agents whose reads fell back to the prior.

    WARNING, not DEBUG: the root logger sits at INFO (app/main.py), so the
    previous debug calls produced no output whatsoever — a Soroban outage
    reverted every agent to the prior, and with it the routing floor, in
    total silence. The line names the agents (capped) and states which way
    the floor is failing, because that is the part that decides who gets
    hired and paid while the chain is unreachable.

    Callers coalesce per batch rather than per agent: the dashboard polls the
    whole registry every 15 s, so one line per agent would be a dozen
    identical warnings per poll for the length of the outage.
    """
    shown = ", ".join(agent_ids[:_DEGRADED_LOG_AGENT_LIMIT])
    if len(agent_ids) > _DEGRADED_LOG_AGENT_LIMIT:
        shown = f"{shown}, +{len(agent_ids) - _DEGRADED_LOG_AGENT_LIMIT} more"
    if prior_clears_floor():
        floor_state = "OPEN — low-reputation agents stay routable until reads recover"
    else:
        floor_state = "CLOSED — prior-scored agents are excluded from routing"
    logger.warning(
        "reputation reads degraded to the prior for %d/%d agents [%s]: %s — routing floor is "
        "failing %s (prior lower bound %d bps vs floor %d bps)",
        len(agent_ids),
        total,
        shown,
        reason,
        floor_state,
        lower_bound_bps(settings.reputation_prior_bps, 0),
        settings.reputation_floor_bps,
    )


async def _read_rep(agent_id: str) -> tuple[RepInfo, str | None]:
    """Core single-agent read: returns (info, failure).

    `failure` is a short description of whatever forced the prior fallback,
    or None. Split out from `fetch_rep` so a batch can decide ONCE whether to
    log instead of emitting a warning per agent.
    """
    if not settings.reputation_enabled or not settings.stellar_reputation_ledger:
        # Reputation switched off, or no ledger deployed. That is a
        # configuration state, not a degradation — nothing to warn about.
        return _prior_info(agent_id), None

    from ..stellar import cache as rcache
    from ..stellar import client as sc

    async def _read() -> dict[str, Any]:
        return await asyncio.to_thread(
            sc.simulate_read,
            sc.contract_ids().reputation_ledger,
            "rep_state",
            [sc.sym(agent_id)],
        )

    try:
        state = await rcache.get_or_set(f"repstate:{agent_id}", settings.reputation_read_ttl_seconds, _read)
    except Exception as e:
        return _prior_info(agent_id, degraded=True), _describe(e)
    if not isinstance(state, dict):
        return _prior_info(agent_id, degraded=True), f"rep_state returned {type(state).__name__}, expected a map"
    return _info_from_state(agent_id, state), None


async def fetch_rep(agent_id: str) -> RepInfo:
    """Read one agent's decayed rep_state from chain; prior on any failure."""
    info, failure = await _read_rep(agent_id)
    if failure is not None:
        _log_degraded([agent_id], 1, failure)
    return info


async def fetch_reps(agent_ids: list[str], timeout_seconds: float = 2.5) -> dict[str, RepInfo]:
    """Concurrent reads for a set of agents, bounded by one overall timeout.

    Never raises: on timeout or error every missing agent falls back to the
    prior, so decompose latency is capped and routing always has a score. A
    batch that degrades logs exactly one warning covering every affected
    agent (see _log_degraded).
    """
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_read_rep(a) for a in agent_ids)),
            timeout=timeout_seconds,
        )
    except Exception as e:  # TimeoutError included: it subclasses Exception on 3.11+
        # The gather was aborted, so nothing is known about any agent.
        _log_degraded(list(agent_ids), len(agent_ids), f"batch read aborted — {_describe(e)}")
        return {a: _prior_info(a, degraded=True) for a in agent_ids}
    failures = [(info.agent_id, failure) for info, failure in results if failure is not None]
    if failures:
        # One line for the batch, reported against the first failure — a
        # single RPC outage produces the same error for every agent.
        _log_degraded([a for a, _ in failures], len(agent_ids), failures[0][1])
    return {info.agent_id: info for info, _ in results}
