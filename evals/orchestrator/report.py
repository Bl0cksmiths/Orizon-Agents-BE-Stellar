"""summary.md and sweep.md for one variant, computed from the files on disk.

Every number is recomputed from results.jsonl and errors.jsonl, never carried
from the runner's memory, so the summary of a resumed run is the summary of
everything on disk. Errors are counted beside the rates, never inside them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import composition, sweep
from . import metrics as m
from .cost import usd
from .dataset import Case, DatasetError, load
from .runner import read_jsonl

_GUARD_ORDER = [
    ("verdict_accuracy", "verdict accuracy"),
    ("injection_recall", "injection recall"),
    ("injection_precision", "injection precision (blocked citing injection)"),
    ("block_recall", "block recall (all should-block)"),
    ("block_precision", "block precision"),
    ("false_block_rate", "false-block rate (legitimate)"),
    ("false_block_rate_security", "false-block rate (borderline security slice)"),
    ("needs_detail_recall", "needs-detail recall"),
    ("false_needs_detail_rate", "false needs-detail rate (legitimate)"),
    ("harmful_recall", "harmful recall (external benchmark)"),
    ("tier_accuracy", "tier accuracy (allowed legit cases)"),
]


def _labelled_cases() -> dict[str, Case]:
    """The in-repo cases by id, for their pipeline labels; none if the set cannot load."""
    try:
        return {c.id: c for c in load()}
    except (DatasetError, OSError):
        return {}


def compare(before_dir: Path, after_dir: Path) -> str:
    """Composition before and after, on the case ids both variants planned."""
    before = read_jsonl(before_dir / "results.jsonl")
    after = read_jsonl(after_dir / "results.jsonl")

    def planned(rows: list[dict[str, Any]]) -> set[str]:
        return {str(r["prompt_id"]) for r in m.ok_rows(rows) if r["meta"].get("plan")}

    shared = planned(before) & planned(after)
    cases = _labelled_cases()
    b = composition.measure([r for r in before if str(r["prompt_id"]) in shared], cases)
    a = composition.measure([r for r in after if str(r["prompt_id"]) in shared], cases)
    lines = [
        "# Planner composition: before and after",
        "",
        f"- before: `{before_dir}`",
        f"- after: `{after_dir}`",
        f"- cases planned by both: {len(shared)}",
        "",
        *composition.compare(b, a),
    ]
    return "\n".join(lines) + "\n"


def summarize(rows: list[dict[str, Any]], errors: list[dict[str, Any]], *, live: bool, pipeline: str) -> str:
    ok = m.ok_rows(rows)
    lines = ["# Orchestrator guard + planner eval", ""]
    if not live:
        lines += [
            f"> **Not a model measurement.** Pipeline `{pipeline}` made no paid call. These numbers test the "
            "harness (runner, grader, metrics), not jev or Claude.",
            "",
        ]
    truncated = sum(1 for r in rows if r.get("status") == "truncated")
    classes: dict[str, int] = {}
    for e in errors:
        classes[e["failure_class"]] = classes.get(e["failure_class"], 0) + 1
    err_txt = ", ".join(f"{k} {v}" for k, v in sorted(classes.items())) or "none"
    fallback = sum(1 for r in ok if r["meta"].get("fallback_served"))
    s = m.spend(rows, errors)
    lines += [
        f"- Pipeline: `{pipeline}`; scored rows: {len(ok)}; truncated (not scored): {truncated}; "
        f"failed attempts (errors.jsonl, not scored): {len(errors)} ({err_txt})",
        f"- Rows answered by a server-side fallback model: {fallback}",
        (
            f"- Measured spend: {usd(s['total_usd'])} ({usd(s['scored_usd'])} on scored rows, "
            f"{usd(s['failed_usd'])} on failed attempts)"
            if live
            else f"- Nothing billed (the fakes' simulated usage prices at {usd(s['total_usd'])})"
        ),
        "",
        "## Guard",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    g = m.guard_metrics(rows)
    for key, label in _GUARD_ORDER:
        lines.append(f"| {label} | {g[key].fmt()} |")
    label, base = m.majority_baseline(rows)
    lines.append(f"| majority-class baseline (always `{label}`) | {base.fmt()} |")

    conf = m.tier_confusion(rows)
    lines += ["", "## Tier confusion (expected rows x routed columns, legit cases)", ""]
    lines.append("| expected \\ routed | " + " | ".join(m.TIER_COLUMNS) + " |")
    lines.append("|---" * (len(m.TIER_COLUMNS) + 1) + "|")
    for exp, row in conf.items():
        lines.append(f"| {exp} | " + " | ".join(str(row[c]) for c in m.TIER_COLUMNS) + " |")
    lines.append("")
    for k, v in m.tier_direction(conf).items():
        lines.append(f"- {k.replace('_', ' ')}: {v.fmt()}")

    p = m.plan_metrics(rows)
    lines += ["", "## Plan validity (raw planner output, before the clamp)", "", "| metric | value |", "|---|---|"]
    for key in ("plan_valid", "plan_allowlisted", "plan_schema", "plan_tiers", "plan_refused"):
        lines.append(f"| {key.replace('_', ' ')} | {p[key].fmt()} |")

    lines += ["", *composition.render(composition.measure(rows, _labelled_cases()))]

    slices: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
        ("Verdict accuracy by category", lambda r: str(r["tags"][0])),
        ("Verdict accuracy by language", lambda r: str(r["tags"][1])),
        ("Verdict accuracy by split", lambda r: str(r.get("split", "-"))),
    ]
    for title, slice_of in slices:
        lines += ["", f"## {title}", "", "| slice | accuracy |", "|---|---|"]
        for k, v in m.by_slice(rows, slice_of).items():
            lines.append(f"| {k} | {v.fmt()} |")

    misses = [r for r in ok if r["grade"].get("verdict_ok") == 0]
    if misses:
        lines += ["", "## Wrong verdicts", "", "| id | expected | got | reasons |", "|---|---|---|---|"]
        for r in sorted(misses, key=lambda r: r["prompt_id"]):
            o = r["meta"]["observed"]
            lines.append(
                f"| {r['prompt_id']} | {r['meta']['expected']['verdict']} | {o['verdict']} | "
                f"{', '.join(o['reasons']) or '-'} |"
            )
    return "\n".join(lines) + "\n"


def write(variant_dir: Path) -> tuple[Path, Path]:
    """Both files, from what is on disk. `run.json` says how the rows were made;
    without it the run is reported as not live, which only adds a caveat."""
    meta_path = variant_dir / "run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    live = bool(meta.get("live", False))
    pipeline = str(meta.get("pipeline", "unknown"))
    rows = read_jsonl(variant_dir / "results.jsonl")
    errors = read_jsonl(variant_dir / "errors.jsonl")
    summary = variant_dir / "summary.md"
    summary.write_text(summarize(rows, errors, live=live, pipeline=pipeline), encoding="utf-8")
    sweep_md = variant_dir / "sweep.md"
    sweep_md.write_text(sweep.render(rows), encoding="utf-8")
    return summary, sweep_md


def headline(variant_dir: Path) -> str:
    rows = read_jsonl(variant_dir / "results.jsonl")
    g = m.guard_metrics(rows)
    p = m.plan_metrics(rows)
    return (
        f"verdict {g['verdict_accuracy'].fmt()} | injection recall {g['injection_recall'].fmt()} | "
        f"false-block {g['false_block_rate'].fmt()} | tier {g['tier_accuracy'].fmt()} | "
        f"plan valid {p['plan_valid'].fmt()}"
    )
