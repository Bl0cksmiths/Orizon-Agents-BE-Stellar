"""
Registry sync — mirrors on-chain AgentRegistry entries into the marketplace.

Closes the registry split-brain (story 1.02): GET /api/agents served only the
in-memory seeded catalog, so an agent registered on-chain — the product's
entire permissionless-marketplace claim — was invisible to the console, the
planner, and the metrics. This module is the missing read path: a background
loop (started from the app lifespan) plus an on-demand `sync_once` that pull
`list_ids` + `get` from the registry contract and upsert the results into
`state.agents`, provenance-tagged `source="onchain"`.

Design notes, each deliberate:

  - Reads are DIRECT and SEQUENTIAL — no `stellar.cache`, no gather. The
    registry is the source of truth being mirrored, so a stale cached read
    only delays the mirror it exists to provide; and each read occupies a
    thread in the shared 8-thread executor, so fanning out one read per agent
    would starve the money-moving paths that share it.
  - The gate reads `settings.stellar_agent_registry` LIVE on every pass and
    never `sc.contract_ids()` — that helper is lru_cached, so it would pin
    whatever id (or blank) it saw first for the life of the process and
    defeat both runtime reconfiguration and hermetic tests.
  - Known ids are re-read every pass, not only new ones: an operator's
    on-chain reprice or delist must propagate to the marketplace — that is
    what makes story 1.08's delist real rather than cosmetic.
  - What the chain reports is UNTRUSTED INPUT, not truth. Registration is
    permissionless and the contract validates neither `name` nor `price`, so
    the API bounds in `RegisterAgentReq` sit on the wrong side of the trust
    boundary and a direct contract call never meets them. The mapper is
    therefore the enforcement point: the name is clamped and neutralised, and
    a price we could never settle — or one too small for reputation to ever
    hold the agent to account — is refused outright, and a refused record
    DELISTS any copy an earlier pass indexed, so the re-read above cannot be
    turned into a way to leave a stale believable price standing.
  - On-chain ids in the `agt_` namespace are SKIPPED: that namespace is the
    seeded catalog, and `state.add_agent` is an upsert, so indexing one would
    clobber a worker-backed agent with a chain record that has no worker.
  - The loop fails OPEN and never dies: a bad pass logs and waits for the
    next tick. Failures coalesce to ONE warning per outage (then DEBUG, then
    an INFO on recovery) — the `reputation_svc._log_degraded` discipline; a
    15s loop against a downed RPC must not flood the log with warnings.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..agents.workers.prompt_safety import sanitize_untrusted
from ..config import settings
from ..schemas import Agent
from ..state import state
from ..stellar import client as sc

logger = logging.getLogger(__name__)

# Floor on the loop cadence — a misconfigured REGISTRY_SYNC_SECONDS of 0 (or
# negative) must degrade to a slow poll, not a hot loop against the RPC.
_MIN_INTERVAL_SECONDS = 5.0

# Bound on a mirrored display name. The chain is NOT a validating peer:
# `AgentRegistry.register` takes `name: String` and stores it verbatim, with no
# length bound and no content check. `RegisterAgentReq.name`'s max_length=100
# binds only callers who came through our API, and registration is
# permissionless — an operator invoking the contract directly never meets it.
# This is the defence-in-depth copy of that bound for the path that bypassed
# it, mirroring the API rule rather than relaxing it (the `MAX_INTENT_CHARS`
# discipline in `prompt_safety`). 100 chars is a display name's worth: long
# enough that no name our own API would have accepted is ever truncated, short
# enough that an essay cannot ride into process memory, the /api/agents
# response, or the planner prompt.
MAX_AGENT_NAME_CHARS = 100

# Bound on a mirrored price, by the same trust-boundary reasoning as the name:
# `register` and `update_price` take a bare `i128` and check nothing about it,
# not even the sign, so `RegisterAgentReq.price_usdc`'s `gt=0, le=10_000` binds
# only callers who came through our API.
MAX_ONCHAIN_PRICE_USDC = 10_000.0

# Floor on a mirrored price — the same trust boundary read from the other end.
# `reputation_svc.rating_weight_stroops` weights a rating by the step's price
# and floors that weight at 1 stroop, while on-chain evidence decays 7.5% per
# epoch (a week). Below some price an agent's negative evidence can never
# outrun the decay, and the routing floor stops being a mechanism at all.
#
# The arithmetic under the shipped config (prior 7000 bps over 12 USDC of prior
# mass, floor 5500 bps, Wilson Z=1): crossing the floor on 20/100 evidence
# takes 0.4481 USDC of decayed rating weight. At this minimum that is 449
# failed steps — or, held against decay, a sustained 34 failures/week, so an
# agent that is failing real traffic is excluded and STAYS excluded. One
# decimal place lower it is 4,482 failures (336/week, forever); at the 1-stroop
# weight floor it is 4,481,328 (336,100/week), which is what "arithmetically
# un-excludable" means in practice.
#
# 0.001 USDC also sits seven times below the cheapest agent in the seeded
# catalog (translate.42 at 0.007, which 65 failures excludes), so this refuses
# a price chosen to be unaccountable, not a price chosen to be cheap.
MIN_ONCHAIN_PRICE_USDC = 0.001


class UnbelievablePrice(ValueError):
    """An on-chain price outside the range we are willing to mirror."""


def _price_ceiling() -> float:
    """Highest per-call price we will believe from the chain.

    The tighter of two bounds, read LIVE on every call rather than cached — the
    `settings.stellar_agent_registry` discipline, so a reconfigured cap takes
    effect on the next pass instead of being pinned for the process lifetime:

      * `MAX_ONCHAIN_PRICE_USDC` — our own API's ceiling, restated on the side
        of the trust boundary that a direct contract call cannot bypass.
      * `settings.max_charge_usdc` — `execution_svc` skips the on-chain charge,
        the seal AND the ratings for the WHOLE RUN when a plan total exceeds
        it. A single step priced above that cap therefore cannot appear in any
        settleable plan: routing to it would not just overcharge, it would
        strip settlement from every honest agent sharing the plan. An agent we
        can never settle is not a cheap agent, it is an unroutable one.
    """
    return min(MAX_ONCHAIN_PRICE_USDC, settings.max_charge_usdc)


def _price_floor() -> float:
    """Lowest per-call price we will believe from the chain.

    A fixed policy minimum rather than a tighter-of-two like `_price_ceiling`:
    nothing in settings bounds a price from below (the API's `price_usdc`
    carries `gt=0`, which admits a single stroop), and the bound that matters
    is the reputation arithmetic recorded on `MIN_ONCHAIN_PRICE_USDC`, not a
    spend cap. Kept as a function anyway, so both bounds are reached the same
    way at the one site that applies them.
    """
    return MIN_ONCHAIN_PRICE_USDC


# Single-flight guard shared by the loop and on-demand callers: overlapping
# passes would race identical reads through the shared executor for no gain.
_lock = asyncio.Lock()

# Handle on the background loop; start()/stop() own its lifecycle.
_task: asyncio.Task | None = None

# Once-per-process log guards. The disabled notice would otherwise repeat
# every tick on a deployment that simply has no registry configured, and a
# squatted agt_ id would re-warn on every pass for as long as it exists
# on-chain.
_disabled_logged = False
_skipped_agt_ids: set[str] = set()
_refused_price_ids: set[str] = set()

# True while the loop is inside a failing streak — flips the pass-failure
# log level from WARNING (first failure) to DEBUG (consecutive), and arms
# the INFO "recovered" line for the next success.
_failing = False


def _describe(e: BaseException) -> str:
    """Compact "Type: message" description, bare type when there is no
    message (asyncio.TimeoutError carries none)."""
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _to_agent(raw: dict[str, Any]) -> Agent:
    """Map one on-chain registry record to the marketplace Agent shape.

    Price arrives as an i128 in stroops (7 decimals). `rep` is the smoothed
    reputation PRIOR on the 0–5 scale (7000 bps → 3.5), not 0: this field
    feeds the metrics trust blend and renders as stars in the console, so a
    zero would both distort the average and paint every newly indexed agent
    as ★0.00 — the opposite of the cold-start policy reputation_svc applies
    everywhere else. `runs` starts at 0 (no execution history is on-chain)
    and `real` stays False: an indexed agent has no in-process worker.

    `name` is the one attacker-controlled free-text field in the record, so it
    is clamped and neutralised HERE, at the trust boundary, rather than only
    where it is consumed: an Agent built by this mapper is held in
    `state.agents`, served by GET /api/agents, and read by every other
    consumer, so an unbounded on-chain string must never enter application
    state in the first place. `id` needs no such treatment (a Soroban `Symbol`
    is `[A-Za-z0-9_]{1,32}` by construction) and neither do `skills`, which are
    a `Vec<Symbol>` on-chain and so cannot carry whitespace, quotes, control
    characters, or anything resembling a fence marker.

    Raises `UnbelievablePrice` for a price outside `[_price_floor(),
    _price_ceiling()]` — REFUSED rather than clamped, at both ends and for the
    same reason. Clamping would invent a commercial term: we would quote the
    buyer, and pay the owner, a rate neither of them agreed to. At the ceiling
    a price clamped to the cap still consumes the entire charge budget and
    still denies settlement to the rest of the plan; at the floor, clamping UP
    would be worse still — we would raise an operator's own published price
    purely to make our scoring arithmetic work. Refusing is the honest failure
    — we cannot represent this agent's terms, so we do not offer it — and
    because the caller then keeps it out of `state.agents` entirely, it is
    unroutable everywhere at once (the planner block, the floor-starvation
    fallback, the substitute search, the model-plan clamp, GET /api/agents and
    the execution pricing) without a price filter duplicated across all six.

    The floor is the dust case (ADR 0005 D4): below it a rating carries so
    little evidence weight that on-chain decay outruns accumulation and the
    routing floor can never exclude the agent, however much it fails. A dust
    price is not a cheap agent, it is an unaccountable one. It also SUBSUMES
    the zero-and-negative check this guard used to spell out — `_price_floor()`
    is positive, so `0` and a negative i128 are still refused, by a strictly
    tighter bound rather than by a second rule sitting next to it.
    """
    price = raw["price"] / 1e7
    floor, ceiling = _price_floor(), _price_ceiling()
    if not floor <= price <= ceiling:
        raise UnbelievablePrice(f"{price:.6f} USDC is outside [{floor:.6f}, {ceiling:.6f}]")
    return Agent(
        id=raw["id"],
        # `sanitize_untrusted` is this repo's existing primitive
        # (app/agents/workers/prompt_safety.py): it strips control characters,
        # defuses fence-marker forgery, and clamps. Deliberately NOT
        # `fence_untrusted` — a name is a field, not a free-text blob, and a
        # multi-line BEGIN/END block cannot live inside an `Agent.name`.
        name=sanitize_untrusted(raw["name"], max_chars=MAX_AGENT_NAME_CHARS),
        skills=list(raw["skills"]),
        price=price,
        rep=settings.reputation_prior_bps / 2000,
        status="online" if raw["active"] else "offline",
        runs=0,
        real=False,
        owner=raw["owner"],
        source="onchain",
    )


def _refuse_price(agent_id: str, reason: UnbelievablePrice) -> None:
    """Keep a refused agent out of state, and DELIST one an earlier pass indexed.

    Eviction is the point: known ids are re-read every pass, so without it an
    operator could register at a believable price, wait to be indexed, then
    reprice into the absurd — this pass would skip the upsert and leave the old
    price standing as marketplace truth, which is precisely the stale mirror
    the re-read exists to prevent. Only non-`agt_` ids reach here (the seeded
    namespace is skipped before this point), so this can never delist a
    worker-backed catalog agent.

    Logged once per id, then at DEBUG — the `_skipped_agt_ids` discipline. A
    15s loop against a permanently over-priced agent must not flood the log.
    The wording is "refusing", not "failed": the record read back perfectly
    well, we decline to believe what it says.
    """
    evicted = state.agents.pop(agent_id, None) is not None
    if agent_id in _refused_price_ids:
        logger.debug("registry sync: still refusing %r — %s", agent_id, reason)
        return
    _refused_price_ids.add(agent_id)
    logger.warning(
        "registry sync: refusing on-chain agent %r — %s; %s",
        agent_id,
        reason,
        "delisted the record a previous pass indexed" if evicted else "not indexed",
    )


async def sync_once() -> int:
    """Run one full sync pass; returns the number of agents upserted.

    Raises on a failed `list_ids` (unknown contract, RPC outage) — the pass
    has nothing to iterate, and the caller owns the failure policy (the loop
    coalesces and survives; an on-demand caller gets the error). A failed
    `get` for ONE id is logged and skipped so a single bad record never
    kills the rest of the pass.
    """
    global _disabled_logged
    async with _lock:
        contract_id = settings.stellar_agent_registry
        if not contract_id:
            if not _disabled_logged:
                _disabled_logged = True
                logger.info("registry sync disabled — STELLAR_AGENT_REGISTRY not set")
            return 0

        ids = await asyncio.to_thread(sc.simulate_read, contract_id, "list_ids", [])
        synced = 0
        for agent_id in ids:
            if agent_id.startswith("agt_"):
                # Seeded namespace: add_agent is an upsert, so indexing this
                # id would clobber a worker-backed catalog agent.
                if agent_id not in _skipped_agt_ids:
                    _skipped_agt_ids.add(agent_id)
                    logger.warning(
                        "registry sync: skipping on-chain id %r — the agt_ namespace is the "
                        "seeded catalog, and upserting it would clobber a worker-backed agent",
                        agent_id,
                    )
                continue
            try:
                raw = await asyncio.to_thread(sc.simulate_read, contract_id, "get", [sc.sym(agent_id)])
                agent = _to_agent(raw)
            except UnbelievablePrice as e:
                # Must precede the catch-all: UnbelievablePrice is a ValueError,
                # and a refusal is a policy decision, not a read failure.
                _refuse_price(agent_id, e)
                continue
            except Exception as e:
                logger.warning("registry sync: failed to index %r: %s", agent_id, _describe(e))
                continue
            # Believable again after a refusal — re-arm the warning so an
            # operator flip-flopping across the cap stays visible in the log.
            _refused_price_ids.discard(agent_id)
            state.add_agent(agent)
            synced += 1
        return synced


async def _sync_loop() -> None:
    """Background loop: sync, wait, repeat — forever.

    Belt over braces: `sync_once` already contains per-id failures, so the
    except here only sees pass-level failures (list_ids, RPC down) — and the
    task must survive those too, because a loop that dies during an outage
    never mirrors the recovery. Consecutive failures coalesce: WARNING once,
    DEBUG until the streak ends, INFO when a pass next succeeds.
    """
    global _failing
    while True:
        try:
            await sync_once()
        except Exception as e:
            if _failing:
                logger.debug("registry sync still failing: %s", _describe(e))
            else:
                _failing = True
                logger.warning(
                    "registry sync pass failed: %s — will keep retrying (coalescing to DEBUG until it recovers)",
                    _describe(e),
                )
        else:
            if _failing:
                _failing = False
                logger.info("registry sync recovered")
        await asyncio.sleep(max(_MIN_INTERVAL_SECONDS, settings.registry_sync_seconds))


def _on_task_done(task: asyncio.Task) -> None:
    # Mirrors execution_svc: a task that dies with an exception would
    # otherwise vanish silently (nothing awaits it).
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("registry sync loop died: %s", exc, exc_info=exc)


def kick() -> None:
    """Fire-and-forget one sync pass — the post-submit fast path (BLO-12).

    A successful registration should appear in the marketplace within
    seconds, not at the next interval. Failures are swallowed at DEBUG:
    the periodic loop retries anyway, and a submit response must never
    wait on (or fail because of) a refresh.
    """

    def _swallow(t: asyncio.Task[int]) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.debug("kicked registry sync failed: %s", t.exception())

    task = asyncio.get_running_loop().create_task(sync_once())
    task.add_done_callback(_swallow)


def start() -> None:
    """Start the background sync loop. Idempotent — a live loop is kept."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_sync_loop())
    _task.add_done_callback(_on_task_done)


async def stop() -> None:
    """Cancel the loop and wait for it to unwind (shutdown path)."""
    global _task
    if _task is None:
        return
    _task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _task
    _task = None
