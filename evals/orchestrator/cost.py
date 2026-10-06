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
IMPROVER_MODEL = "claude-sonnet-5-5"
PLANNER_MODEL = "claude-opus-5-5"

# Stage sizes in tokens. Overheads are the fixed text around the intent: the
# fence and security directive, the system prompt, the four guard questions,
# the AVAILABLE_AGENTS block for the 12 seeded agents.
FENCE_OVERHEAD = 150
GUARD_QUESTIONS = 450  # four questions with their criteria text
IMPROVER_SYSTEM = 900
IMPROVER_OUTPUT = 1_500  # Spec + adaptive thinking at the improver's effort
IMPROVER_MAX_TOKENS = 4_000
SPEC_TOKENS = 400  # the Spec as re-checked and as handed to the planner
PLANNER_SYSTEM = 1_200
AGENT_BLOCK = 700
PLANNER_OUTPUT_BY_EFFORT = {"low": 2_000, "medium": 5_000, "high": 10_000}  # plan + thinking
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
    model: str, input_tokens: int, output_tokens: int, table: dict[str, dict[str, float]] | None = None
) -> float:
    p = (table or prices())[model]
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


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


def case_estimate(case: Case, *, stages: str) -> tuple[dict[str, float], float]:
    """(expected cost by stage, ceiling) for one case, one rep."""
    table = prices()
    intent = estimate_tokens(case.intent) + FENCE_OVERHEAD
    by_stage = {"guard": call_cost(GUARD_MODEL, intent + GUARD_QUESTIONS, 0, table)}
    ceiling = by_stage["guard"]
    if stages == "all" and case.expected_verdict == "allow":
        tier = case.expected_tier or "complex"
        effort = EFFORT_FOR_TIER[tier]
        improve_in = IMPROVER_SYSTEM + intent
        by_stage["improve"] = call_cost(IMPROVER_MODEL, improve_in, IMPROVER_OUTPUT, table)
        by_stage["recheck"] = call_cost(GUARD_MODEL, SPEC_TOKENS + intent + GUARD_QUESTIONS, 0, table)
        plan_in = PLANNER_SYSTEM + AGENT_BLOCK + SPEC_TOKENS + intent
        by_stage["plan"] = call_cost(PLANNER_MODEL, plan_in, PLANNER_OUTPUT_BY_EFFORT[effort], table)
        ceiling += (
            call_cost(IMPROVER_MODEL, improve_in, IMPROVER_MAX_TOKENS, table)
            + by_stage["recheck"]
            + call_cost(PLANNER_MODEL, plan_in, PLANNER_MAX_TOKENS[tier], table)
        )
    return by_stage, ceiling


def estimate(cases: Iterable[Case], *, stages: str, reps: int = 1) -> Estimate:
    totals: dict[str, float] = {}
    ceiling = 0.0
    n = planned = 0
    for c in cases:
        n += 1
        by_stage, cap = case_estimate(c, stages=stages)
        planned += "plan" in by_stage
        ceiling += cap
        for k, v in by_stage.items():
            totals[k] = totals.get(k, 0.0) + v
    totals = {k: v * reps for k, v in totals.items()}
    return Estimate(n, planned, reps, sum(totals.values()), ceiling * reps, totals)
