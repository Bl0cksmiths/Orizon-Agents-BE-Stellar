"""
Reputation service — Bayesian-smoothed, evidence-weighted agent reputation.

Division of labour (mirrors ERC-8004: raw evidence on-chain, aggregation off):

  - The ReputationLedger contract stores decayed, value-weighted rating
    evidence per agent (`rep_state` → sum_w / weight / count / disputed).
  - This service applies the Bayesian prior (Jøsang-style beta smoothing),
    derives a conservative normal-approximation (Wald) lower bound for the
    routing floor — long called "Wilson" here, and still exposed as
    `wilson_z`, but it is not the Wilson score interval (see
    `lower_bound_bps`),
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
evidence: if a fresh read does not answer — the chain is unreachable, or the
batch deadline passes first — the caller gets the agent's last known on-chain
read, marked `stale=True` with its age, while one younger than the read TTL
plus REPUTATION_STALE_GRACE_SECONDS exists; otherwise the prior, marked
`source="prior"` and `degraded=True`. A read a landed rating has made obsolete
(`invalidate_rep`) is the exception on both counts: it is served at any age,
marked `superseded` as well, and the routing floor refuses it until a read
taken after the rating answers — so a just-disputed agent is shown its last
real score and routed on nothing, never on the prior.

Degradation policy (deliberate, not accidental) — when an agent's ledger
read cannot be had and there is no recent read to serve stale, that agent
falls back to the prior and the routing floor therefore fails OPEN for it
under the shipped config: a prior-only agent clears the floor, so a
low-reputation agent becomes routable while it lasts. That is kept, for
three reasons:

  - It is the same position the system takes on any agent it knows nothing
    about. An unreadable ledger genuinely means "reputation unknown", and
    the product's answer to unknown reputation is "routable" (cold start).
  - Failing closed would not fail closed. Every agent would drop below the
    floor at once, tripping the orchestrator's _MIN_ROUTABLE_AGENTS backstop,
    which then picks a top-N by identical prior scores — the same agents get
    hired, with less defined semantics.
  - It is narrow, and per agent. Each read is judged on its own under the
    batch deadline, so one slow agent degrades only itself; an agent read in
    the last TTL + REPUTATION_STALE_GRACE_SECONDS (315 s shipped) is served
    that read, stale, which the floor still judges; and a read cut off by the
    deadline keeps running and fills the cache for the next batch. What is
    left failing open is an agent with no read that recent: never read yet
    (the boot pre-warm covers the registry once the sync's first pass is in,
    or its boot bound has passed), or a chain unreachable for longer than the
    grace. An agent a rating has just landed on is never among them: its last
    read is kept, superseded, and refused (`invalidate_rep`).

It is NOT bounded in time. Earlier notes here claimed the window was bounded
by the read TTL and the batch timeout; both audits disproved that. The TTL
only decides when a read is next attempted and the deadline only how long a
batch waits for it, so a chain that stays unreachable (or slower than the
deadline) keeps every agent past its grace on the prior for as long as that
lasts — loudly, one WARNING per batch.

What was actually broken is that none of this was visible: the fallbacks
logged at DEBUG under an INFO root logger, i.e. not at all. Every fallback
now marks the RepInfo `degraded` and emits one WARNING per batch that names
the agents and states which way the floor is failing (`_log_degraded`).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, PrivateAttr

from ..config import settings

logger = logging.getLogger(__name__)

STROOPS_PER_USDC = 10_000_000
# One-sided ~84% confidence. Deliberately gentle: paired with the prior mass
# it lets a prior-only newcomer clear the floor, while real negative evidence
# still drags the bound down fast. The name is historical — the bound is a
# normal-approximation (Wald) bound, not Wilson's (`lower_bound_bps`) — and it
# is kept because /reputation/params publishes it as `wilson_z`.
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
    # True when these numbers are the agent's LAST KNOWN on-chain read, served
    # because a fresh read did not answer in time (the batch deadline passed or
    # the read failed) — not a prior. Every other field is that earlier read's
    # evidence, scored exactly as it was then, and the routing floor is applied
    # to it. Distinct from `degraded`, which means "no evidence was available,
    # so the prior stands in and the floor fails open for this agent". A stale
    # row is never degraded; a degraded row is never stale.
    stale: bool = False
    # Seconds since the served evidence was read from the ledger. Set only
    # when `stale`, else None. Never above the read TTL plus
    # REPUTATION_STALE_GRACE_SECONDS on a row the floor judges; a superseded
    # row (below) is served at any age, because the floor refuses it whatever
    # its age.
    stale_age_seconds: float | None = None
    # True on a stale row whose read PREDATES a rating that has since landed:
    # `invalidate_rep` ran after it was read, so the ledger now holds evidence
    # these numbers do not include. Shown rather than dropped, so a score never
    # jumps to the prior the moment a dispute lands (6983 → 7000 was the S8
    # defect), but never routed on: `passes_floor` refuses it until a read
    # taken after the rating answers. Only ever set together with `stale`.
    #
    # A private attribute behind a read-only property, not a field: the read
    # routes answer through a mirror model (routers/stellar.py ReputationInfo)
    # that has to declare every field, and the flag is the planner's business.
    # On the wire the row reads as what it is to a viewer — `stale`, with its
    # age. Private attributes survive `model_copy`, so a row keeps the flag
    # through any copy the planner makes of it.
    _superseded: bool = PrivateAttr(default=False)

    @property
    def superseded(self) -> bool:
        return self._superseded


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
    """Normal-approximation (Wald) lower bound on the smoothed mean:
    p - z * sqrt(p(1-p)/n).

    Not the Wilson score interval, whatever the constant's name says: there
    is no score correction, so at p -> 1 (or 0) the bound collapses onto the
    mean. The prior's mass counts toward n as if it were observed jobs. Both
    are as tuned — the cold-start margin is built on exactly this arithmetic —
    so it is named for what it is rather than changed.

    Effective sample size counts prior mass plus on-chain evidence in units
    of one-USDC jobs, so confidence grows with settled value, not raw count.
    """
    n = (prior_weight_stroops() + max(weight, 0)) / STROOPS_PER_USDC
    if n <= 0:
        return 0
    p = min(max(mean_bps / 10_000, 0.0), 1.0)
    lb = p - WILSON_Z * math.sqrt(p * (1.0 - p) / n)
    return max(0, min(10_000, round(lb * 10_000)))


class ColdStartMargin(BaseModel):
    """The cold-start arithmetic as data, so no caller has to redo it.

    Every field is a number a log line — or an operator reading one — would
    otherwise have to derive from source. That derivation is exactly how the
    margin became folklore: the single thing keeping permissionless
    registration honest is a subtraction nobody can see.
    """

    floor_bps: int  # REPUTATION_FLOOR_BPS, as this process actually has it
    prior_bps: int  # REPUTATION_PRIOR_BPS — the mean a newcomer is credited
    prior_weight_usdc: float  # REPUTATION_PRIOR_WEIGHT_USDC — its evidence mass
    lower_bound_bps: int  # what a prior-only agent is scored on for routing
    margin_bps: int  # lower_bound_bps - floor_bps; negative excludes newcomers
    clears: bool


def cold_start_margin() -> ColdStartMargin:
    """What a newly registered agent scores, against the floor it must clear.

    `prior_clears_floor()` answers the same question with a bool, which is all
    a line about an in-progress outage needs. This reports the numbers behind
    it, because cold-start routability is a MARGIN rather than a property.
    With the shipped config a prior-only agent scores 5677 bps against a 5500
    bps floor, and those 177 bps are the whole of what stops open registration
    being theatre: the floor is applied to the prior-smoothed lower bound, not
    to the raw on-chain mean (which is 0 for an unrated agent), so a newcomer
    is judged on the prior and clears.

    Raise reputation_floor_bps past that bound, or lower reputation_prior_bps
    or reputation_prior_weight_usdc, and the margin goes negative: every new
    agent misses the floor on its first request, is never routed, therefore is
    never rated, and an unrated agent never leaves the prior — so the
    exclusion is permanent, not a slow start. Nothing fails when it happens.
    Reads succeed, the floor is applied exactly as written, registration keeps
    accepting agents, and the marketplace quietly stops hiring anyone new.

    Returning the numbers rather than the verdict is the point: a caller given
    only a bool has to re-derive prior, weight and bound to say anything
    actionable, which puts a second copy of this arithmetic in the place least
    able to keep it in step with this one.
    """
    bound = lower_bound_bps(settings.reputation_prior_bps, 0)
    floor = settings.reputation_floor_bps
    return ColdStartMargin(
        floor_bps=floor,
        prior_bps=settings.reputation_prior_bps,
        prior_weight_usdc=settings.reputation_prior_weight_usdc,
        lower_bound_bps=bound,
        margin_bps=bound - floor,
        # `>=`, mirroring passes_floor: an agent sitting exactly ON the floor
        # clears it, so a floor set equal to the prior bound still admits
        # newcomers — with zero margin, which the numbers above make visible.
        clears=bound >= floor,
    )


def prior_clears_floor() -> bool:
    """Whether a prior-only agent clears the routing floor under the current
    config — i.e. whether `passes_floor` fails OPEN when the ledger is
    unreadable and every agent degrades to the prior.

    True with the shipped defaults (prior lower bound 5677 bps vs a 5500 bps
    floor). Read at log time rather than hard-coded, so an operator who
    raises REPUTATION_FLOOR_BPS above the prior bound gets a floor that fails
    closed AND a warning line that says so, instead of either policy being a
    silent property of the arithmetic. See the module docstring.

    Delegates to `cold_start_margin()` rather than repeating its comparison:
    the same two numbers decide the degradation policy here and whether a
    newcomer is routable at all, and two copies of that eventually disagree —
    at which point the outage warning and the startup warning would describe
    different deployments.
    """
    return cold_start_margin().clears


def passes_floor(info: RepInfo | None) -> bool:
    """Routing-floor check on the conservative lower bound.

    Degraded (prior-fallback) infos are NOT special-cased: they are judged by
    the same arithmetic as a cold-start agent, so the configured floor stays
    the single authority on what is routable. With the default config that
    means an outage fails open — deliberately, and now loudly logged by
    `_log_degraded`; see the module docstring for why.

    A `superseded` row IS special-cased, and fails closed: its numbers predate
    a rating that has landed since, so its lower bound is a claim about an
    agent the ledger has already moved — downward, after a dispute. Judging it
    would route a just-disputed agent on its pre-dispute score; failing it
    means that agent waits for one fresh read, which `invalidate_rep` has
    already started. The window is the time a read takes, not a TTL.
    """
    if info is None:
        return True
    if info.superseded:
        return False
    return info.lower_bound_bps >= settings.reputation_floor_bps


def max_rating_weight_usdc() -> float:
    """The most evidence weight ONE rating can carry, in USDC.

    REPUTATION_MAX_RATING_TO_PRIOR_RATIO times the prior's weight, inside the
    absolute REPUTATION_MAX_RATING_WEIGHT_USDC. Tied to the prior because the
    prior is what one rating has to be weighed against: at a ratio of 1 a
    single rating can at most equal the prior, so it pulls an agent's score at
    most halfway toward itself — a 95/100 whale job lands a newcomer at 8250,
    not 9232 — and it takes more than one job to overrule the prior. The
    absolute cap alone was 100 USDC against a 12 USDC prior, which let one
    self-dealt run at the price ceiling set the score outright.
    """
    by_prior = settings.reputation_max_rating_to_prior_ratio * settings.reputation_prior_weight_usdc
    return min(settings.reputation_max_rating_weight_usdc, by_prior)


def rating_weight_stroops(step_price_usdc: float) -> int:
    """Evidence weight of one rating: the step's QUOTED price, capped at
    `max_rating_weight_usdc()`.

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
    capped = min(max(step_price_usdc, 0.0), max_rating_weight_usdc())
    return max(1, round(capped * STROOPS_PER_USDC))


def _carries_checkable_work(step_output: dict[str, Any]) -> bool:
    """Whether a response delivered anything this service can actually check.

    The two signals in the published external envelope that are evidence
    rather than assertion: an `artifact`, which is the thing the buyer
    receives, and critic (or validator) content, which is the record of a
    pass made over that work. A response carrying neither has told us only
    that an HTTP handler is alive.

    Deliberately NOT a quality grader (ADR 0005 D3): it never reads the
    artifact, scores prose, or weighs the violations. It answers the single
    question a rating must settle before it can award the base score — was
    anything delivered — and nothing else.
    """
    if step_output.get("artifact"):
        return True
    # `critic_violations` only. The fallback to `validator_violations` that
    # used to live here read a key the external response contract drops — it is
    # not on the allowlist — so an operator who sent that name had it silently
    # discarded AND was then scored as having delivered nothing. Two stories
    # written by different hands, each correct alone. A first-party worker that
    # sets `validator_violations` is unaffected: this gate only runs for
    # untrusted output (ADR 0005 D3).
    return isinstance(step_output.get("critic_violations"), list)


def synthetic_rating(
    step_output: dict[str, Any] | None,
    step_price_usdc: float,
    *,
    first_party: bool = True,
) -> tuple[int, int]:
    """Derive the settler's synthetic rating for one step.

    Returns (rating_0_to_100, weight_stroops). The rating is built from
    verifiable workflow signals (did the worker produce output, did it ship
    an artifact, did the critic find violations) — validation-gated
    reputation rather than opinion. Baked kit artifacts are deterministic
    and pre-validated by design, so they earn a fixed high score.

    `first_party` is the trust distinction `execution_svc._rating_view`
    already draws: was this step run by the worker this repo registers for
    the agent id, or by an operator's endpoint. It is keyword-only and
    defaults to True so a first-party step scores byte-identically to how it
    always has; the settler passes False for a bound external step.

    For UNTRUSTED output only, a response must carry something checkable to
    reach the base score — an acknowledgement scores as non-delivery (ADR
    0005 D3). Base 70 is exactly `reputation_prior_bps`, so before this gate
    `{"ok": true}` held the mean at the prior while growing the evidence
    mass, and the lower bound therefore ROSE with every junk response
    (5677 → 5746 over 25 of them): answering garbage forever scored strictly
    better than failing honestly, and no volume of it could ever cross the
    routing floor. First-party scoring is untouched — a local worker
    legitimately returns text with no artifact, and regrading that is a
    different and much larger economic change.
    """
    weight = rating_weight_stroops(step_price_usdc)

    if not step_output:
        # Timed out, raised, or returned nothing at all. NO money settled:
        # every failure path skips the billing site (ADR 0005 D1), so this is
        # unbilled negative evidence, and that is exactly the mechanism — the
        # buyer keeps the money and the operator takes the reputation cost.
        return 20, weight

    if not first_party and not _carries_checkable_work(step_output):
        # An acknowledgement that delivers nothing is worth what a timeout is
        # worth, because the buyer received the same thing either way — so it
        # scores the same 20. Not a middle value: anything above 20 leaves a
        # lie priced cheaper than honest failure, and anything below it would
        # punish a junk answer harder than a dead endpoint, which is not a
        # distinction the evidence supports.
        #
        # Placed ahead of the `baked` branch as well as the base score: 95 is
        # above base, so the same rule has to gate it. `_rating_view` already
        # strips `source` from untrusted output; this is a second lock on that
        # door, not a replacement for it.
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


_STATE_FIELDS = ("sum_w", "weight", "count", "disputed")


def _checked_state(state: Any) -> tuple[int, int, int, int]:
    """(sum_w, weight, count, disputed) from a rep_state map, or raise.

    The ledger is trusted to write sane evidence, and this is what stops a
    state it did NOT write sanely from scoring as evidence. Without it a
    negative weight scored the maximum — smoothed and lower bound both 10000 —
    and a sum_w above 10000 x weight reported an average of 10^12 bps. Neither
    is a reputation; both are a contract bug or a redeploy mid-migration, and
    the honest reading of either is "this read failed". Raising makes it one:
    inside the cache's producer the state is negatively cached for the short
    failure window instead of stored for the TTL, and `_read_rep` degrades the
    agent to the prior with the reason in the batch warning.

    A missing field reads as 0, as it always has. Every present field must be
    a non-negative integer — not a bool, not a float, so a NaN or an infinity
    can never reach the arithmetic — and the evidence mean sum_w / weight must
    sit on the 0..10_000 scale.
    """
    if not isinstance(state, dict):
        raise TypeError(f"rep_state returned {type(state).__name__}, expected a map")
    values: list[int] = []
    for field in _STATE_FIELDS:
        value = state.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"rep_state.{field} is {type(value).__name__}, expected an integer")
        if value < 0:
            raise ValueError(f"rep_state.{field} is negative")
        values.append(value)
    sum_w, weight, count, disputed = values
    if sum_w > 10_000 * weight:
        raise ValueError("rep_state.sum_w exceeds 10000 x weight, a mean above the rating scale")
    return sum_w, weight, count, disputed


def _info_from_state(agent_id: str, state: dict[str, Any]) -> RepInfo:
    """Score one rep_state map. Raises on a state `_checked_state` refuses."""
    sum_w, weight, count, disputed = _checked_state(state)
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


def _log_stale(infos: list[RepInfo], total: int, reason: str) -> None:
    """ONE warning for the agents served their last known on-chain value.

    Not `_log_degraded`: nothing here fails open. These agents are still
    judged on real evidence, only older than the TTL, so the line says how
    old — the oldest of them — rather than which way the floor is failing.
    Superseded ones are not judged at all but refused (`passes_floor`), and
    the line says so rather than claiming the floor weighed their evidence.
    """
    ids = [info.agent_id for info in infos]
    shown = ", ".join(ids[:_DEGRADED_LOG_AGENT_LIMIT])
    if len(ids) > _DEGRADED_LOG_AGENT_LIMIT:
        shown = f"{shown}, +{len(ids) - _DEGRADED_LOG_AGENT_LIMIT} more"
    oldest = max(info.stale_age_seconds or 0.0 for info in infos)
    superseded = sum(1 for info in infos if info.superseded)
    if not superseded:
        verdict = "the routing floor is still applied to that evidence"
    elif superseded == len(infos):
        verdict = "each was rated since that read, so the routing floor refuses it until a fresh read answers"
    else:
        verdict = (
            f"the routing floor is still applied to that evidence, except for the {superseded} rated since "
            "that read, which it refuses until a fresh read answers"
        )
    logger.warning(
        "reputation reads served the last known on-chain value for %d/%d agents [%s]: %s — %s (oldest read %.1f s ago)",
        len(ids),
        total,
        shown,
        reason,
        verdict,
        oldest,
    )


def _report(outcomes: list[tuple[RepInfo, str]], total: int) -> None:
    """Log a batch's fallbacks: at most one stale line and one degraded line.

    `outcomes` holds every agent whose fresh read did not answer, with the
    reason. A single RPC outage produces the same error for every agent, so
    each line is reported against its first reason.
    """
    stale = [(info, reason) for info, reason in outcomes if info.stale]
    degraded = [(info, reason) for info, reason in outcomes if not info.stale]
    if stale:
        _log_stale([info for info, _ in stale], total, stale[0][1])
    if degraded:
        _log_degraded([info.agent_id for info, _ in degraded], total, degraded[0][1])


def _stale_info(agent_id: str) -> RepInfo | None:
    """The agent's last known on-chain read, marked stale — or None.

    None when there is no stored read, when it expired more than
    REPUTATION_STALE_GRACE_SECONDS ago, or when it does not score (it was
    stored before a check that now refuses it). The age is measured from
    when the read was stored: its expiry minus the read TTL.

    A read `invalidate_rep` has superseded is served whatever its age, marked
    `superseded`. The grace bounds how old evidence the floor may JUDGE, and
    the floor judges none of this — it refuses it (`passes_floor`) — so the
    grace has nothing to protect here. What it would do is swap the agent's
    real, older score for the prior, which clears the floor: the one agent a
    dispute just landed on would become routable because of it.
    """
    from ..stellar import cache as rcache

    last = rcache.last_stored(_rep_cache_key(agent_id))
    if last is None:
        return None
    past_expiry = time.monotonic() - last.expiry
    if not last.superseded and past_expiry > settings.reputation_stale_grace_seconds:
        return None
    try:
        info = _info_from_state(agent_id, last.value)
    except Exception:
        return None
    age = max(0.0, settings.reputation_read_ttl_seconds + past_expiry)
    served = info.model_copy(update={"stale": True, "stale_age_seconds": round(age, 1)})
    served._superseded = last.superseded
    return served


def _fallback(agent_id: str) -> RepInfo:
    """What an agent is scored on when its fresh read did not answer: its
    last known on-chain read if one is recent enough, else the prior marked
    degraded."""
    return _stale_info(agent_id) or _prior_info(agent_id, degraded=True)


_pool: ThreadPoolExecutor | None = None
_oversize_logged = False


def _read_pool() -> ThreadPoolExecutor:
    """The worker threads reserved for rep_state reads.

    Reserved, not shared: on the default executor a batch queued behind the
    registry sync, the ratings writer and every other read in the process,
    so the time it took depended on traffic it had no part in. Sized by
    REPUTATION_READ_CONCURRENCY, the number the config validator checks the
    batch deadline against. Created on first use.
    """
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=settings.reputation_read_concurrency, thread_name_prefix="repread")
    return _pool


def shutdown_read_pool() -> None:
    """Release the read threads (lifespan shutdown). A read still running is
    abandoned, not joined; the next read creates a fresh pool."""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _note_batch_size(size: int) -> None:
    """Say once per process when a batch outgrows what its deadline was sized
    for — the validator can only check the size it was told to expect."""
    global _oversize_logged
    if size > settings.reputation_batch_agents and not _oversize_logged:
        _oversize_logged = True
        logger.warning(
            "reputation batch of %d agents is larger than REPUTATION_BATCH_AGENTS=%d, the size its deadline "
            "was sized for — its last wave of reads may miss the deadline on a healthy chain. Raise "
            "REPUTATION_READ_CONCURRENCY or REPUTATION_BATCH_AGENTS (and the deadline with it).",
            size,
            settings.reputation_batch_agents,
        )


def _rep_cache_key(agent_id: str) -> str:
    """The read cache's key for one agent's rep_state.

    Built in exactly one place because two callers depend on agreeing about
    it: `_read_rep` fills it and `invalidate_rep` drops it. A key that drifted
    between them would turn invalidation into a silent no-op — the pre-rating
    score keeps being served, and nothing fails.
    """
    return f"repstate:{agent_id}"


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
        # load_source=False: a view read needs no sequence number, so the
        # load_account hop is skipped and each read is ONE round trip, not two
        # (client.simulate_read). That halves the per-read latency the batch
        # deadline has to cover.
        raw = await asyncio.get_running_loop().run_in_executor(
            _read_pool(),
            partial(
                sc.simulate_read,
                sc.contract_ids().reputation_ledger,
                "rep_state",
                [sc.sym(agent_id)],
                load_source=False,
            ),
        )
        # Refused HERE, inside the producer, so the cache records a failure —
        # negatively cached for its short window and retried after it — rather
        # than storing the bad payload as a success that every hit then reads
        # as degraded for the full TTL. `simulate_read` returns None for an
        # empty result set, which is the reachable case; an out-of-range state
        # is refused the same way.
        _checked_state(raw)
        return raw

    try:
        state = await rcache.get_or_set(_rep_cache_key(agent_id), settings.reputation_read_ttl_seconds, _read)
        return _info_from_state(agent_id, state), None
    except Exception as e:
        return _fallback(agent_id), _describe(e)


async def fetch_rep(agent_id: str) -> RepInfo:
    """Read one agent's decayed rep_state from chain. On a failed read: its
    last known on-chain value marked stale if recent enough, else the prior
    marked degraded."""
    info, failure = await _read_rep(agent_id)
    if failure is not None:
        _report([(info, failure)], 1)
    return info


def _wait_bound(bound: float) -> float | None:
    """The batch deadline as `asyncio.wait` takes it.

    Settings refuses a bound that is not a positive, finite number, but the
    explicit argument is not validated, and `asyncio.wait` would schedule a NaN
    deadline rather than refuse it. Anything not above zero — NaN included —
    therefore expires on arrival, exactly as `wait_for` treated it, and inf is
    no deadline at all.
    """
    if not bound > 0:
        return 0
    if math.isinf(bound):
        return None
    return bound


async def fetch_reps(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
    """Concurrent reads for a set of agents, each judged on its own.

    One task per agent under one shared deadline (`asyncio.wait`). Every read
    that has answered by the deadline is KEPT — a cache hit, a fresh on-chain
    read, or a failure it already degraded — and only the agents whose read is
    still pending fall back. The old shape, one `wait_for` around a `gather`,
    was all-or-nothing: a single slow agent threw away every other agent's
    evidence, including answers already served from cache in microseconds, and
    a known sub-floor agent was then routed on the prior.

    A pending read is cancelled, but cancelling it abandons only THIS batch's
    wait: `_read_rep` awaits its cache flight through `asyncio.shield`, so the
    RPC read keeps running and lands in the cache for the next reader.

    Never raises, so decompose latency is capped and routing always has a
    score. A batch that degrades logs exactly one warning covering every
    affected agent (see _log_degraded).
    """
    # None means "whatever the deployment is configured for". The bound lives
    # in Settings rather than in this signature so a config validator can see
    # it: `_reputation_read_fits_the_planning_budget` refuses a boot where this
    # read could eat the planning budget, and a validator that cannot read the
    # value it is validating is theatre. An explicit argument still wins —
    # the timeout-path tests drive it directly.
    bound = settings.reputation_batch_timeout_seconds if timeout_seconds is None else timeout_seconds
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    _note_batch_size(len(ids))
    tasks = {agent_id: asyncio.ensure_future(_read_rep(agent_id)) for agent_id in ids}
    try:
        await asyncio.wait(tasks.values(), timeout=_wait_bound(bound))
    finally:
        # Also on the way out of a cancelled caller, so no read outlives the
        # batch that asked for it (the flight under it still lands).
        pending_tasks = [task for task in tasks.values() if not task.done()]
        for task in pending_tasks:
            task.cancel()

    infos: dict[str, RepInfo] = {}
    outcomes: list[tuple[RepInfo, str]] = []
    for agent_id, task in tasks.items():
        if task in pending_tasks:
            # Cut off by the deadline: its last known on-chain read if it has
            # a recent one, which the floor can still judge, else the prior.
            info = _fallback(agent_id)
            outcomes.append((info, f"read still pending at the {bound:g} s batch deadline"))
        else:
            info, failure = task.result()
            if failure is not None:
                outcomes.append((info, failure))
        infos[agent_id] = info
    if outcomes:
        _report(outcomes, len(ids))
    return infos


_prewarm_task: asyncio.Task[None] | None = None


async def _prewarm() -> None:
    from ..state import state

    ids = [agent.id for agent in state.list_agents()]
    # No batch deadline: nothing is waiting on this read, and each RPC call is
    # still bounded by the read client's own timeout. A read that fails is
    # logged like any other batch's, and simply is not cached.
    infos = await fetch_reps(ids, timeout_seconds=math.inf)
    warmed = sum(1 for info in infos.values() if not info.degraded)
    logger.info("reputation cache pre-warmed: %d/%d agents read from the ledger", warmed, len(ids))


def start_prewarm() -> None:
    """Read every registered agent's reputation once, in the background, at boot.

    Without it the first plan after a deploy is the first reader of every
    agent: a whole cold batch — new TLS sessions, a waking RPC — racing one
    deadline, and whatever it cuts off has no earlier read to serve stale, so
    it is routed on the prior. Started in the background so the request that
    woke the instance is not held behind the chain; a plan that arrives while
    it runs joins its in-flight reads instead of issuing its own. Reads the
    registry as it stands once boot has waited, bounded, for the registry
    sync's first pass (`registry_sync.wait_first_pass`), so on-chain agents
    are read too; only agents a pass indexes after that bound are read on
    first use. A no-op while reputation is off or no ledger is configured.
    """
    global _prewarm_task
    if not settings.reputation_enabled or not settings.stellar_reputation_ledger:
        return
    if _prewarm_task is not None and not _prewarm_task.done():
        return
    _prewarm_task = asyncio.get_running_loop().create_task(_prewarm())
    _prewarm_task.add_done_callback(_on_prewarm_done)


def _on_prewarm_done(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("reputation pre-warm died: %s", _describe(task.exception()))  # type: ignore[arg-type]


async def stop_prewarm() -> None:
    """Cancel a pre-warm still running (shutdown path)."""
    global _prewarm_task
    task, _prewarm_task = _prewarm_task, None
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task})


# Post-rating refreshes in progress, one per agent. Bounded, so a burst of
# ratings (a whole plan's steps settling at once is the common case) cannot
# become an unbounded number of tasks; past the bound a refresh is skipped,
# which only means the agent's next reader goes to the ledger itself — the
# superseded entry already forces that.
_MAX_REFRESHES = 64
_refreshes: dict[str, asyncio.Task[None]] = {}
# Agents invalidated AGAIN while their refresh was running. That refresh is
# awaiting a read the second invalidation detached — one from before the
# second rating — so it goes round once more instead of stopping on it.
_refresh_again: set[str] = set()


async def _refresh(agent_id: str) -> None:
    """Read one agent back from the ledger after a rating landed, no deadline.

    Through `_read_rep`, so the read is the cache's single flight for the key:
    a plan or a page that asks meanwhile joins it rather than issuing its own,
    and whoever it answers, its result is what the cache holds next. Bounded
    by the read client's own RPC timeout, like the boot pre-warm's reads.
    """
    while True:
        _refresh_again.discard(agent_id)
        _info, failure = await _read_rep(agent_id)
        if failure is not None:
            # Nobody is waiting on this read, so it is not a batch to warn
            # about: the next reader retries it and reports it if it still fails.
            logger.debug("post-rating reputation refresh for %s failed: %s", agent_id, failure)
        if agent_id not in _refresh_again:
            return


def _on_refresh_done(agent_id: str, task: asyncio.Task[None]) -> None:
    if _refreshes.get(agent_id) is task:
        del _refreshes[agent_id]
    if not task.cancelled() and task.exception() is not None:
        logger.error("post-rating reputation refresh for %s died: %s", agent_id, _describe(task.exception()))  # type: ignore[arg-type]


def _schedule_refresh(agent_id: str) -> None:
    """Start reading `agent_id` back from the ledger now, in the background.

    Never blocks and never raises: `invalidate_rep` runs on rating paths that
    must not wait on, or fail because of, a read. Single-flight per agent — an
    agent already being refreshed is marked to go round again rather than
    given a second task — and bounded by `_MAX_REFRESHES`. Nothing to do
    without a running event loop (a synchronous caller): the superseded entry
    already sends the next reader to the ledger.
    """
    if not settings.reputation_enabled or not settings.stellar_reputation_ledger:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    running = _refreshes.get(agent_id)
    # Same loop too: a task left pending on a loop that has since closed will
    # never run again, and must not stand in for a refresh on this one.
    if running is not None and not running.done() and running.get_loop() is loop:
        _refresh_again.add(agent_id)
        return
    if len(_refreshes) >= _MAX_REFRESHES:
        logger.debug("post-rating reputation refresh for %s skipped: %d already running", agent_id, len(_refreshes))
        return
    task = loop.create_task(_refresh(agent_id))
    _refreshes[agent_id] = task
    task.add_done_callback(partial(_on_refresh_done, agent_id))


async def stop_refreshes() -> None:
    """Cancel the post-rating refreshes still running (shutdown path).

    Cancelling a refresh abandons only its wait: the read under it runs in the
    reputation read pool, which `shutdown_read_pool` releases after this.
    """
    tasks = [task for task in _refreshes.values() if not task.done()]
    _refreshes.clear()
    _refresh_again.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks)


def invalidate_rep(agent_id: str) -> None:
    """Retire the cached rep_state for one agent, so the next read goes back
    to the ledger.

    Call it once a rating for the agent has LANDED — a dispute rating above
    all. Without it every reader keeps the pre-rating score for up to
    `reputation_read_ttl_seconds`, and a plan decomposed inside that window is
    routed and stamped on the very number the dispute was meant to change. A
    read already in flight when this runs is detached rather than cancelled,
    and cannot write its pre-rating result back (`app.stellar.cache.invalidate`).

    The stored read is KEPT, marked superseded, not dropped. Dropping it was
    the S8 defect: a next read that missed the batch deadline then had no last
    known value to fall back on and served the prior — 6983 became 7000,
    `source=prior`, `degraded=true` — so the agent a dispute had just landed on
    cleared the floor and its score went UP on the agents page. Kept, it is
    what that slow read serves instead (`_stale_info`): the last on-chain
    numbers, `stale` with their age and `superseded`, which `passes_floor`
    refuses. The page shows a real, older score; the planner routes nothing
    on it. A read that answers — this one's own refresh below, or any other —
    replaces it, and the flag with it.

    And the read is started HERE, not left to the next reader
    (`_schedule_refresh`): in the background, single-flight, bounded, never
    blocking the rating path that called this. A plan or a page load that
    arrives while it runs joins it; one that arrives after it finds the
    post-rating value already cached. So the window in which a just-rated
    agent is held off routing is one ledger read, started at the moment the
    rating landed, rather than whenever somebody next asked and then the
    batch deadline on top.

    This one key is enough because it is the only cache in front of
    reputation: `fetch_rep`, `fetch_reps`, both /api/stellar/reputation routes,
    the decompose snapshot and the dashboard's trust average all read through
    `_read_rep`. Nothing downstream memoises a RepInfo — every decompose takes
    a fresh `fetch_reps` snapshot, and the stamps on a plan already stored
    record what THAT plan was judged on, not a score anything reuses.
    """
    from ..stellar import cache as rcache

    rcache.invalidate(_rep_cache_key(agent_id))
    _schedule_refresh(agent_id)
