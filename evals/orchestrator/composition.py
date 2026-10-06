"""Pipeline composition: how a raw plan composes the platform's specialists.

The planner is asked to compose real pipelines (research → brief → copy →
design → build → review → seal, and the other recipes in
`app/agents/orchestrator.py`) instead of defaulting to one code step, and never
to pad a plan with steps that add nothing. Three measures, all on the RAW plan
(before the clamp and the composition rules), so they describe what the model
proposed:

* **distinct specialists per plan** — how many different agents a plan uses
  (mean, distribution, by category), with the single-step rate beside it;
* **recipe coverage** — on cases with a pipeline label (`dataset.PipelineLabel`),
  the share of the labelled `expect` agents the plan uses, and how often it
  uses all of them, by recipe;
* **irrelevant-step rate** — on labelled cases, steps whose agent is in
  neither `expect` nor `optional`, over all their steps.

Plus two rule checks: the labelled agents a plan uses appear in the label's
handoff order, and a plan never buys both code builders.

Labels are read from the dataset by case id, not from the run rows, so a run
made before the labels existed can be re-scored (`compare`).
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .dataset import Case, PipelineLabel
from .metrics import Rate, ok_rows

BUILDERS = frozenset({"code.gen", "code.next"})


def agent_names() -> dict[str, str]:
    """Agent id → name for the built-in catalog (ids outside it are kept as ids)."""
    from app.seed import _SEED

    return {row[0]: row[1] for row in _SEED}


def plan_names(raw: Mapping[str, Any] | None, names: Mapping[str, str]) -> list[str] | None:
    """The plan's step agents by name, in order; None when there is no usable plan."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("steps"), list):
        return None
    out = []
    for step in raw["steps"]:
        if not isinstance(step, Mapping):
            return None
        agent_id = str(step.get("agent_id"))
        out.append(names.get(agent_id, agent_id))
    return out


def in_order(names: Sequence[str], expect: Sequence[str]) -> bool:
    """Whether the labelled agents the plan uses first appear in the label's order."""
    first: dict[str, int] = {}
    for i, n in enumerate(names):
        first.setdefault(n, i)
    positions = [first[n] for n in expect if n in first]
    return positions == sorted(positions)


def checks(names: Sequence[str], label: PipelineLabel | None) -> dict[str, int]:
    """Binary per-case composition grades (left out where they do not apply)."""
    used = set(names)
    out = {"plan_one_builder": int(not BUILDERS <= used)}
    if label is not None and names:
        out["plan_recipe_full"] = int(set(label.expect) <= used)
        out["plan_no_irrelevant"] = int(used <= label.relevant)
        out["plan_order_ok"] = int(in_order(names, label.expect))
    return out


@dataclass
class Composition:
    plans: int = 0
    distinct: list[int] = field(default_factory=list)
    distinct_by_category: dict[str, list[int]] = field(default_factory=dict)
    single_step: Rate = Rate(0, 0)
    labelled: int = 0
    coverage: list[float] = field(default_factory=list)
    coverage_by_recipe: dict[str, list[float]] = field(default_factory=dict)
    full_by_recipe: dict[str, Rate] = field(default_factory=dict)
    full_coverage: Rate = Rate(0, 0)
    irrelevant_steps: Rate = Rate(0, 0)
    irrelevant_by_recipe: dict[str, Rate] = field(default_factory=dict)
    irrelevant_agents: Counter[str] = field(default_factory=Counter)
    order_ok: Rate = Rate(0, 0)
    one_builder: Rate = Rate(0, 0)

    @property
    def distinct_mean(self) -> float | None:
        return statistics.fmean(self.distinct) if self.distinct else None

    @property
    def coverage_mean(self) -> float | None:
        return statistics.fmean(self.coverage) if self.coverage else None


def _add(rate: Rate, hits: int, n: int) -> Rate:
    return Rate(rate.hits + hits, rate.n + n)


def measure(
    rows: Iterable[dict[str, Any]], cases: Mapping[str, Case], names: Mapping[str, str] | None = None
) -> Composition:
    """Composition measures over every scored row with a raw plan."""
    names = agent_names() if names is None else names
    out = Composition()
    for r in ok_rows(list(rows)):
        steps = plan_names(r["meta"].get("plan"), names)
        if not steps:
            continue
        case = cases.get(str(r["prompt_id"]))
        used = set(steps)
        out.plans += 1
        out.distinct.append(len(used))
        out.distinct_by_category.setdefault(str(r["tags"][0]), []).append(len(used))
        out.single_step = _add(out.single_step, int(len(steps) == 1), 1)
        out.one_builder = _add(out.one_builder, int(not BUILDERS <= used), 1)
        label = case.pipeline if case is not None else None
        if label is None:
            continue
        out.labelled += 1
        share = len(set(label.expect) & used) / len(label.expect)
        full = int(set(label.expect) <= used)
        out.coverage.append(share)
        out.coverage_by_recipe.setdefault(label.recipe, []).append(share)
        out.full_coverage = _add(out.full_coverage, full, 1)
        out.full_by_recipe[label.recipe] = _add(out.full_by_recipe.get(label.recipe, Rate(0, 0)), full, 1)
        stray = [n for n in steps if n not in label.relevant]
        out.irrelevant_agents.update(stray)
        out.irrelevant_steps = _add(out.irrelevant_steps, len(stray), len(steps))
        out.irrelevant_by_recipe[label.recipe] = _add(
            out.irrelevant_by_recipe.get(label.recipe, Rate(0, 0)), len(stray), len(steps)
        )
        out.order_ok = _add(out.order_ok, int(in_order(steps, label.expect)), 1)
    return out


def _num(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def render(c: Composition) -> list[str]:
    """The composition section of summary.md."""
    hist = Counter(c.distinct)
    lines = [
        "## Pipeline composition (raw planner output)",
        "",
        f"- Plans: {c.plans}; distinct specialists per plan: mean {_num(c.distinct_mean)}, "
        f"distribution {', '.join(f'{k}: {v}' for k, v in sorted(hist.items())) or '-'}",
        f"- Single-step plans: {c.single_step.fmt()}",
        f"- Plans that buy both code builders: {Rate(c.one_builder.n - c.one_builder.hits, c.one_builder.n).fmt()}",
        f"- Labelled cases: {c.labelled}; mean recipe coverage {_pct(c.coverage_mean)}; "
        f"full coverage {c.full_coverage.fmt()}",
        f"- Irrelevant-step rate (labelled cases): {c.irrelevant_steps.fmt()}",
        f"- Labelled agents in handoff order: {c.order_ok.fmt()}",
        "",
        "| recipe | cases | mean coverage | full coverage | irrelevant steps |",
        "|---|---|---|---|---|",
    ]
    for recipe in sorted(c.coverage_by_recipe):
        shares = c.coverage_by_recipe[recipe]
        lines.append(
            f"| {recipe} | {len(shares)} | {_pct(statistics.fmean(shares))} | {c.full_by_recipe[recipe].fmt()} | "
            f"{c.irrelevant_by_recipe[recipe].fmt()} |"
        )
    lines += ["", "| category | plans | mean distinct specialists |", "|---|---|---|"]
    for category, counts in sorted(c.distinct_by_category.items()):
        lines.append(f"| {category} | {len(counts)} | {statistics.fmean(counts):.2f} |")
    if c.irrelevant_agents:
        top = ", ".join(f"{name} {n}" for name, n in c.irrelevant_agents.most_common())
        lines += ["", f"Irrelevant steps by agent: {top}"]
    return lines


def compare(before: Composition, after: Composition, *, title: str = "Before / after") -> list[str]:
    """Two composition measures side by side (same cases, two planner versions)."""

    def row(label: str, b: str, a: str) -> str:
        return f"| {label} | {b} | {a} |"

    lines = [f"## {title}", "", "| measure | before | after |", "|---|---|---|"]
    lines.append(row("plans", str(before.plans), str(after.plans)))
    lines.append(row("mean distinct specialists", _num(before.distinct_mean), _num(after.distinct_mean)))
    lines.append(row("single-step plans", before.single_step.fmt(), after.single_step.fmt()))
    lines.append(row("mean recipe coverage", _pct(before.coverage_mean), _pct(after.coverage_mean)))
    lines.append(row("full recipe coverage", before.full_coverage.fmt(), after.full_coverage.fmt()))
    lines.append(row("irrelevant-step rate", before.irrelevant_steps.fmt(), after.irrelevant_steps.fmt()))
    lines.append(row("labelled agents in handoff order", before.order_ok.fmt(), after.order_ok.fmt()))
    lines.append(row("plans with one code builder at most", before.one_builder.fmt(), after.one_builder.fmt()))
    for recipe in sorted(set(before.coverage_by_recipe) | set(after.coverage_by_recipe)):
        b = before.coverage_by_recipe.get(recipe)
        a = after.coverage_by_recipe.get(recipe)
        lines.append(
            row(
                f"coverage: {recipe}",
                _pct(statistics.fmean(b)) if b else "n/a",
                _pct(statistics.fmean(a)) if a else "n/a",
            )
        )
    for category in sorted(set(before.distinct_by_category) | set(after.distinct_by_category)):
        b2 = before.distinct_by_category.get(category)
        a2 = after.distinct_by_category.get(category)
        lines.append(
            row(
                f"distinct specialists: {category}",
                f"{statistics.fmean(b2):.2f}" if b2 else "n/a",
                f"{statistics.fmean(a2):.2f}" if a2 else "n/a",
            )
        )
    return lines
