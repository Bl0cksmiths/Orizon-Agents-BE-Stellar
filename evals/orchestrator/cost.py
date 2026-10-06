"""A per-run spend estimate, printed before any live call is made.

No `count_tokens` call and no tokenizer: the estimate has to exist before the
run is allowed to spend anything, including on counting. Tokens are estimated
from characters, deliberately on the high side (3 ASCII characters per token,
one token per non-ASCII character, which is about right for CJK and generous
for accented Latin text); every per-stage figure is a named constant below so
the assumption is inspectable, and the first live run's measured `usage` (in
`results.jsonl` and `summary.md`) is what should replace them.

Two numbers per run:

* `expected` — the stage sizes below, with output (thinking included) at the
  typical figure for the planner's effort.
* `ceiling` — every call writing its full `max_tokens`. `--max-usd` is checked
  against `expected` before the run starts, and against MEASURED spend while it
  runs: the runner stops starting cases once measured spend plus one more
  case's ceiling would pass the cap.

Planning only runs for cases the guard allows, so the estimate counts the
improve + re-check + plan stages for the cases EXPECTED to be allowed; a guard
that allows more costs more, which the in-run meter catches.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from .dataset import Case

# $/MTok — orchestrator-v2's price table (`app/llm/spend.py` once it lands; it
# then wins, see `prices()`). jev bills input only.
_FALLBACK_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10},
    "jev": {"input": 0.042, "output": 0.0, "cache_read": 0.0},
}

GUARD_MODEL = "jev"
FALLBACK_MODEL = "claude-haiku-4-5"
IMPROVER_MODEL = "claude-sonnet-5-5"
PLANNER_MODEL = "claude-opus-5-5"

# Stage sizes in tokens. Overheads are the fixed text around the intent: the
# fence and security directive, the system prompt, the four guard questions,
# the AVAILABLE_AGENTS block for the 12 seeded agents.
FENCE_OVERHEAD = 150
GUARD_QUESTIONS = 1_100  # the four questions with their criteria (≈1,220 tokens per call measured)
FALLBACK_SYSTEM = 1_800  # the fallback's rules plus the rendered question battery
FALLBACK_OUTPUT = 120
FALLBACK_MAX_TOKENS = 400  # intent_guard._FALLBACK_MAX_TOKENS
IMPROVER_SYSTEM = 900
IMPROVER_OUTPUT = 600  # Spec + thinking: 319 tokens on average measured, doubled for headroom
IMPROVER_MAX_TOKENS = 4_000
SPEC_TOKENS = 400  # the Spec as re-checked and as handed to the planner
# The pipeline-composing instructions (recipes, handoff rules) and the
# AVAILABLE_AGENTS block with a role card under each of the 12 built-in agents.
PLANNER_SYSTEM = 1_600
AGENT_BLOCK = 1_600
# Plan + thinking. Measured 2026-10-06 (88 plans, mostly one or two steps): max
# 148 / 239 / 509 output tokens at effort low / medium / high. Plans are now
# 3-6 step pipelines with 30-word rationales, so these allow about three times
# the old maxima until a live run measures them again.
PLANNER_OUTPUT_BY_EFFORT = {"low": 500, "medium": 800, "high": 1_400}
# Cache writes bill at 1.25x input; both system prompts are written once per
# run (per rep: a cache entry outlives one case, not a long gap) and read at
# the cache-read rate after that — measured on the 2026-10-06 run, where the
# planner read 1,936 cached tokens and the improver 924 on every call.
CACHE_WRITE_MULTIPLIER = 1.25
PLANNER_MAX_TOKENS = {"low": 4_000, "moderate": 8_000, "complex": 16_000}  # the planner's own budgets
EFFORT_FOR_TIER = {"low": "low", "moderate": "medium", "complex": "high"}


def usd(amount: float) -> str:
    """Dollars, with enough digits that a guard-only run does not read as $0.00."""
    return f"${amount:.2f}" if amount >= 1 else f"${amount:.4f}"


def prices() -> dict[str, dict[str, float]]:
    """The app's own price table when it exists, so a price change cannot leave
    the estimate on a stale rate; this module's copy until then."""
    try:
        from app.llm import spend
    except ImportError:
        return _FALLBACK_PRICES
    out = {
        model: {"input": p.input, "output": p.output, "cache_read": p.cache_read}
        for model in (PLANNER_MODEL, IMPROVER_MODEL, "claude-haiku-4-5")
        for p in (spend.price_for(model),)
    }
    out[GUARD_MODEL] = {"input": spend.JEV_INPUT_USD_PER_MTOK, "output": 0.0, "cache_read": 0.0}
    return out


def estimate_tokens(text: str) -> int:
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_chars / 3) + (len(text) - ascii_chars)


def call_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    table: dict[str, dict[str, float]] | None = None,
    *,
    cached_tokens: int = 0,
) -> float:
    """List price of one call; `cached_tokens` of input billed at the cache-read rate."""
    p = (table or prices())[model]
    return (input_tokens * p["input"] + cached_tokens * p["cache_read"] + output_tokens * p["output"]) / 1_000_000


@dataclass(frozen=True)
class Estimate:
    cases: int
    planned_cases: int
    reps: int
    expected_usd: float
    ceiling_usd: float
    by_stage_usd: dict[str, float]

    def describe(self) -> str:
        stages = ", ".join(f"{k} {usd(v)}" for k, v in self.by_stage_usd.items())
        return (
            f"{self.cases} cases x {self.reps} rep(s), {self.planned_cases} expected to reach planning: "
            f"expected ~{usd(self.expected_usd)}, ceiling {usd(self.ceiling_usd)} ({stages})"
        )


def reservation(case: Case, *, stages: str, fallback: bool = False) -> float:
    """What the runner holds back before starting `case`: its ceiling as if
    the guard allowed it. A guard that misses lets an injection through to
    the planner, and that case must not be able to spend past the cap."""
    return case_estimate(case, stages=stages, assume_allowed=True, fallback=fallback)[1]


def case_estimate(
    case: Case, *, stages: str, assume_allowed: bool = False, fallback: bool = False
) -> tuple[dict[str, float], float]:
    """(expected cost by stage, ceiling) for one case, one rep. `fallback`
    prices the guard as its Claude Haiku 4.5 stand-in instead of jev."""
    table = prices()
    intent = estimate_tokens(case.intent) + FENCE_OVERHEAD
    if fallback:
        guard_in = FALLBACK_SYSTEM + intent
        by_stage = {"guard": call_cost(FALLBACK_MODEL, guard_in, FALLBACK_OUTPUT, table)}
        ceiling = call_cost(FALLBACK_MODEL, guard_in, FALLBACK_MAX_TOKENS, table)
    else:
        by_stage = {"guard": call_cost(GUARD_MODEL, intent + GUARD_QUESTIONS, 0, table)}
        ceiling = by_stage["guard"]
    if stages == "all" and (assume_allowed or case.expected_verdict == "allow"):
        tier = case.expected_tier or "complex"
        effort = EFFORT_FOR_TIER[tier]
        improve_in = IMPROVER_SYSTEM + intent
        # Expected: the system prompts come from the prompt cache; the ceiling
        # below assumes they do not.
        by_stage["improve"] = call_cost(IMPROVER_MODEL, intent, IMPROVER_OUTPUT, table, cached_tokens=IMPROVER_SYSTEM)
        by_stage["recheck"] = call_cost(GUARD_MODEL, SPEC_TOKENS + intent + GUARD_QUESTIONS, 0, table)
        plan_in = PLANNER_SYSTEM + AGENT_BLOCK + SPEC_TOKENS + intent
        by_stage["plan"] = call_cost(
            PLANNER_MODEL,
            SPEC_TOKENS + intent,
            PLANNER_OUTPUT_BY_EFFORT[effort],
            table,
            cached_tokens=PLANNER_SYSTEM + AGENT_BLOCK,
        )
        ceiling += (
            call_cost(IMPROVER_MODEL, improve_in, IMPROVER_MAX_TOKENS, table)
            + by_stage["recheck"]
            + call_cost(PLANNER_MODEL, plan_in, PLANNER_MAX_TOKENS[tier], table)
        )
    return by_stage, ceiling


def cache_write_usd(table: dict[str, dict[str, float]] | None = None) -> float:
    """Writing the improver's and the planner's system prompts to the cache, once."""
    table = table or prices()
    return (
        CACHE_WRITE_MULTIPLIER
        * (
            IMPROVER_SYSTEM * table[IMPROVER_MODEL]["input"]
            + (PLANNER_SYSTEM + AGENT_BLOCK) * table[PLANNER_MODEL]["input"]
        )
        / 1_000_000
    )


def estimate(cases: Iterable[Case], *, stages: str, reps: int = 1, fallback: bool = False) -> Estimate:
    totals: dict[str, float] = {}
    ceiling = 0.0
    n = planned = 0
    for c in cases:
        n += 1
        by_stage, cap = case_estimate(c, stages=stages, fallback=fallback)
        planned += "plan" in by_stage
        ceiling += cap
        for k, v in by_stage.items():
            totals[k] = totals.get(k, 0.0) + v
    if planned:
        totals["cache_write"] = cache_write_usd()
    totals = {k: v * reps for k, v in totals.items()}
    return Estimate(n, planned, reps, sum(totals.values()), ceiling * reps, totals)
