"""
Per-agent failure tracking — one counter per agent, so a persistently broken
endpoint is visible in the platform log without reading every trace (story
2.03, ADR 0005).

Nothing in `app/` counted per-agent outcomes before this. The run loop logs
every failed step with a full traceback (`exc_info=True`) and moves on, so an
endpoint that is simply DOWN writes one traceback per step per run — five on a
five-step plan, forever, on a 512 MB instance — and an operator reading that
log still cannot answer the only question worth asking: is this one bad run, or
is that agent broken? This module answers it in one line and coalesces the
rest.

Three properties are load-bearing rather than incidental:

  - BOUNDED, because agent ids are caller-influenced. Registration is
    permissionless and binding is open to anyone, so a map keyed by agent id
    with no cap is a memory-exhaustion vector reachable by whoever can get a
    step routed to them. The shape is `app/pdax/ramp_store.py`'s: an
    `OrderedDict` with a hard cap and an explicit eviction on the insert that
    would exceed it — never expiry-only reclamation, which `app/stellar/cache.py`
    spells out is not enough when a stream of unique keys can be fresh and
    still unbounded. Only FAILURES allocate: a success for an agent with no
    streak is a no-op, so the map is bounded by the agents that have actually
    failed rather than by the agents that exist.
  - COALESCED ON FAILURE-CLASS CHANGE, not on a bare "is failing" flag. The
    pattern is `dispatch_signing._warned_reason`, keyed by reason precisely so
    a deployment that changes failure MODE is still told once. It transfers
    exactly: an endpoint going connection-refused → HTTP 500 → schema
    rejection tells the operator something new each time, while the same class
    repeating does not — and each class is news only the FIRST time a streak
    shows it, so an endpoint alternating between two of them cannot flip the
    guard back into a flood. `registry_sync._refuse_price` supplies the other half —
    per-id keying, and re-arming on recovery so an agent flip-flopping across
    the line stays visible instead of being coalesced away forever.
  - NOTHING OPERATOR-CONTROLLED REACHES A LOG LINE. `rule` is a token from a
    closed, lowercase snake_case vocabulary and an agent id is bounded by
    `AGENT_ID_PATTERN`; anything else is normalised or refused BEFORE it is
    stored, so no URL, host, header, or exception text can be smuggled in
    through either argument. That is ADR 0003's logging rule ("refusals log
    the host and rule, not the attacker-controlled full URL") applied one
    layer further in, and `app/config.py`'s deliberate swallowing of a
    malformed-key exception is the precedent for withholding a value rather
    than interpolating it hopefully.

Concurrency: every function here is synchronous and contains no `await`, so
two workflows running concurrently under `orchestrator_max_concurrent` cannot
interleave inside one of them — the event loop only switches tasks at a
suspension point, and there is none. With `--workers 1` (render.yaml) that
makes the read-modify-write sequences below atomic without a lock, the same
reasoning `app/state.py` and `app/stellar/cache.py` rely on. Callers must not
hop this onto a worker thread (`asyncio.to_thread`): the check-then-evict in
`record_failure` is not atomic under true parallelism, and the module would
need a `threading.Lock` to become so.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass

from ..schemas import AGENT_ID_PATTERN

logger = logging.getLogger(__name__)

# Retention cap for tracked agents (oldest-eviction on insert, see _evict_one).
# Sized against what can legitimately be in here rather than against a memory
# budget: only agents that have FAILED and not yet succeeded occupy a slot, and
# the seeded catalog is twelve. A deployment with 256 distinct agents in a
# failing streak at once is already an anomaly worth an eviction line, and the
# whole map at capacity is a few tens of KB — in the same range as the house's
# other caps (state.py 200 tasks, ramp_store.py 500 ramps, cache.py 512 keys).
_MAX_AGENTS = 256

# Consecutive failures that promote a same-class streak back to WARNING, once.
# Seven, because the orchestrator decomposes an intent into 1–6 steps
# (app/agents/orchestrator.py): a streak that reaches 7 cannot be explained by
# one unlucky run against a plan that used the same agent for every step, so
# crossing it is the first evidence that the endpoint is broken ACROSS runs —
# which is exactly the "persistently broken" the story asks to make visible.
_ESCALATION_STREAK = 7

# Failure classes are lowercase snake_case tokens from a closed vocabulary
# (ADR 0005's DISPATCH_RULES). Validated by SHAPE and not by membership: the
# vocabulary lives in a worker module, and the ADR is explicit that the failure
# path must not import a worker to classify a failure. The bound is what keeps
# an unclassified exception's attributes out of the log, not the spelling.
_RULE_SHAPE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# What an unusable class becomes. `pdax.errors.orizon_code`'s "pdax_error"
# discipline: an undocumented code degrades to one generic token rather than
# being passed through. Collapsing every unusable value to a SINGLE token is
# also what stops class-change coalescing from being turned inside out — a
# caller who could vary the rule freely could otherwise force a WARNING on
# every failure by changing it each time, making the flood guard the flood.
_UNCLASSIFIED_RULE = "unclassified"


@dataclass
class _Streak:
    """One agent's current run of failures. Absent means "not failing"."""

    # Most recent failure class, so the next failure can tell a change from a
    # repeat.
    rule: str
    # Consecutive failures since the last success, across classes — a changed
    # class is still a failure, so it extends the streak rather than resetting
    # it. This is what consecutive_failures() returns.
    count: int
    # Classes already reported for this streak. A change is news the FIRST time
    # it is seen, not every time: a struggling endpoint alternating between,
    # say, a connect failure and a read timeout changes class on every single
    # step, and warning on each one would turn the coalescing guard back into
    # the flood it prevents. The set is bounded by the closed rule vocabulary
    # (everything unusable collapses to one token), and it dies with the
    # streak — `registry_sync`'s "once per id, then DEBUG, re-armed on
    # recovery" discipline, narrowed to once per class per outage.
    seen: set[str]
    # True once the run-length WARNING has fired for this streak, so it fires
    # once per streak and not on every failure past the threshold.
    escalated: bool = False


_streaks: OrderedDict[str, _Streak] = OrderedDict()

# True while the map is full and evicting. Flips the eviction line from WARNING
# (the first drop) to DEBUG, and is re-armed when a success frees a slot — the
# `registry_sync._failing` discipline, needed here for the same reason: at
# capacity EVERY new agent evicts one, so an uncoalesced warning would be a
# log flood proportional to the churn, which is the failure mode this module
# exists to prevent.
_at_capacity = False


def record_failure(agent_id: str, rule: str) -> None:
    """Count one failed step for `agent_id`, classified as `rule`.

    Both arguments are treated as untrusted. `rule` is normalised to the
    closed vocabulary's shape; an agent id outside `AGENT_ID_PATTERN` is not
    tracked at all — it cannot name an agent the loop could have dispatched to
    (the seeded catalog is `agt_…`, and every binding and registry path bounds
    the id with that pattern), so it is a planner bug or a probe, and both are
    better dropped than given a slot in a bounded map and a line in the log.
    """
    if not re.fullmatch(AGENT_ID_PATTERN, agent_id):
        # Nothing is interpolated — not even truncated. The value is the
        # operator-controlled part, and `Settings._report_malformed_stellar_signing_key`
        # is the precedent: name the field, withhold the value. DEBUG because
        # this is unreachable through the routed path, and a caller who could
        # reach it at will would otherwise have a free log-flood.
        logger.debug("failure tracker: ignoring a step failure whose agent id is outside AGENT_ID_PATTERN")
        return
    failure_class = _normalize_rule(rule)
    streak = _streaks.get(agent_id)
    if streak is None:
        if len(_streaks) >= _MAX_AGENTS:
            _evict_one()
        _streaks[agent_id] = _Streak(rule=failure_class, count=1, seen={failure_class})
        # First failure of a streak: the one line an operator needs to see the
        # moment an agent starts failing, and the anchor everything after it
        # coalesces against.
        logger.warning(
            "agent %s failed a step (%s) — 1 consecutive (coalescing to DEBUG until the class changes)",
            agent_id,
            failure_class,
        )
        return
    # Touched agents move to the young end, so eviction drops the agent that
    # has been quiet longest rather than the one that merely started failing
    # first — the deviation from ramp_store, which can rank by a record's own
    # terminal status; here recency is the only evidence of disposability.
    _streaks.move_to_end(agent_id)
    streak.count += 1
    previous, streak.rule = streak.rule, failure_class
    if failure_class not in streak.seen:
        # A class this streak has not shown before is new operator information
        # even mid-outage: the endpoint that was refusing connections is now
        # answering and failing validation, which is a different fix. The
        # streak is NOT reset — a different way of failing is still failing.
        streak.seen.add(failure_class)
        logger.warning(
            "agent %s failure class changed: %s → %s — %d consecutive (coalescing to DEBUG for classes "
            "already reported in this streak)",
            agent_id,
            previous,
            failure_class,
            streak.count,
        )
        return
    if streak.count >= _ESCALATION_STREAK and not streak.escalated:
        # Once per streak, not once per failure past the line: the count is in
        # the DEBUG lines and readable through consecutive_failures(), so a
        # second escalation would carry no information the first did not.
        streak.escalated = True
        logger.warning(
            "agent %s has failed %d consecutive steps (latest class: %s) — the endpoint looks "
            "persistently broken, not merely flaky (coalescing to DEBUG again)",
            agent_id,
            streak.count,
            failure_class,
        )
        return
    # Same class, again: the flood case the module exists to absorb. One line
    # per failed step is what the run loop already writes with a traceback;
    # this one is cheap, greppable, and off by default.
    logger.debug("agent %s still failing: %s — %d consecutive", agent_id, failure_class, streak.count)


def record_success(agent_id: str) -> None:
    """Clear `agent_id`'s failure streak. A no-op if it had none.

    Deliberately allocates nothing: the success path is the common path, and
    inserting a zero-count entry per successful step would make the map track
    every agent that has ever run instead of only the ones that are failing.
    """
    global _at_capacity
    streak = _streaks.pop(agent_id, None)
    if streak is None:
        return
    # A slot is free again, so the next eviction is news rather than churn.
    _at_capacity = False
    # Exactly one INFO, and only where a streak really ended —
    # `registry_sync`'s "recovered" line and `dispatch_signing._report_signed`
    # both fire off the path that actually succeeded, so a persistent failure
    # can never be made to alternate WARNING/INFO forever. The id is safe to
    # interpolate by construction: record_failure is the only writer and it
    # refuses anything outside AGENT_ID_PATTERN, so a key that exists passed
    # that gate.
    logger.info(
        "agent %s recovered after %d consecutive failures (last class: %s)",
        agent_id,
        streak.count,
        streak.rule,
    )


def consecutive_failures(agent_id: str) -> int:
    """Failures since `agent_id` last succeeded, 0 when it is not failing.

    Pure: it does not touch eviction order, so polling this (a metrics route
    would) cannot reshuffle which agent is dropped next.
    """
    streak = _streaks.get(agent_id)
    return streak.count if streak is not None else 0


def _normalize_rule(rule: str) -> str:
    """The failure class to store and log, or the generic token."""
    return rule if _RULE_SHAPE.fullmatch(rule) else _UNCLASSIFIED_RULE


def _evict_one() -> None:
    """Drop the least recently failing agent to make room for a new one.

    Says so, because the drop is not free: the evicted agent's streak restarts
    from zero, so its next failure reports as a first failure and its run-length
    escalation is deferred. That is `ramp_store._evict_one` logging an evicted
    in-flight ramp rather than losing it silently — an operator reading a "1
    consecutive" line deserves to know a counter was reset under them.
    """
    global _at_capacity
    agent_id, streak = _streaks.popitem(last=False)
    if _at_capacity:
        logger.debug("failure tracker still full: dropped %s (%s, %d consecutive)", agent_id, streak.rule, streak.count)
        return
    _at_capacity = True
    logger.warning(
        "failure tracker is full at %d agents — dropped the least recently failing one, %s (%s, %d consecutive); "
        "its streak restarts from zero (coalescing to DEBUG until a success frees a slot)",
        _MAX_AGENTS,
        agent_id,
        streak.rule,
        streak.count,
    )
