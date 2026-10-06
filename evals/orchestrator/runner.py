"""Run cases through a pipeline, grade them, and write each row as it completes.

Layout (one flow directory, report-builder compatible):

    <flow>/_state.json                    metric declarations + train/test ids
    <flow>/<variant>/results.jsonl        one row per scored (case, rep)
    <flow>/<variant>/errors.jsonl         one row per attempt that produced nothing scorable
    <flow>/<variant>/traces/<id>_rep<k>.json

Properties the numbers depend on:

* Rows are appended as each case finishes, so a crash costs only the cases in
  flight, and a re-run resumes at the (case, rep) key — it skips exactly what
  results.jsonl already holds and never writes a duplicate rep.
* A failed attempt (guard unavailable, model unavailable after its retries,
  the per-case ceiling, a harness exception, the app's spend cap) goes to
  errors.jsonl with its class and any billed usage, never into results.jsonl:
  an unavailable guard is not a guard that blocked, and a row there would make
  resume skip the case forever.
* Each case has a hard wall-clock ceiling. The timed-out call may keep running
  in the background; the ceiling reclaims the slot and the case is recorded as
  a timeout.
* No case-level retries: a case is scored on its one attempt. Transient-error
  retries live in the app's model layer, where production has them too.
* Spend is metered from each call's measured cost. A case is not started when
  measured spend, plus what cases in flight may still spend, plus this case's
  ceiling (as if the guard allowed it) would pass `max_usd`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cost
from .contract import CaseRun, Pipeline, PipelineUnavailable, SpendCapReached, StageCall
from .dataset import Case, assign_splits
from .grading import grade

METRICS = [
    {"id": "verdict_ok", "label": "verdict", "kind": "binary"},
    {"id": "tier_ok", "label": "tier", "kind": "binary"},
    {"id": "plan_valid", "label": "plan valid", "kind": "binary"},
    {"id": "plan_allowlisted", "label": "allowlisted", "kind": "binary"},
    {"id": "plan_schema", "label": "schema", "kind": "binary"},
    {"id": "plan_tiers", "label": "step tiers", "kind": "binary"},
]
PERF_FIELDS = [
    {"id": "cost_usd", "label": "cost", "unit": "$"},
    {"id": "latency_s", "label": "latency", "unit": "s"},
    {"id": "in_tokens", "label": "in tok"},
    {"id": "out_tokens", "label": "out tok"},
]


@dataclass
class RunConfig:
    flow_dir: Path
    variant: str = "baseline"
    reps: int = 1
    stages: str = "guard"  # guard | all
    concurrency: int = 4
    timeout_s: float = 180.0
    max_usd: float | None = None  # None: no cap (only allowed when nothing is live)

    @property
    def variant_dir(self) -> Path:
        return self.flow_dir / self.variant


@dataclass
class RunOutcome:
    scored: int = 0
    errors: int = 0
    skipped_resume: int = 0
    skipped_budget: int = 0
    spent_usd: float = 0.0
    stopped: str | None = None  # why the run stopped starting cases, if it did
    error_classes: dict[str, int] = field(default_factory=dict)


class RunRefused(Exception):
    """The run would mix incompatible rows, or spend without a cap."""


class _Meter:
    """Measured spend plus what in-flight cases may still spend."""

    def __init__(self, cap: float | None) -> None:
        self.cap = cap
        self.spent = 0.0
        self.reserved = 0.0

    def try_reserve(self, ceiling: float) -> bool:
        if self.cap is not None and self.spent + self.reserved + ceiling > self.cap:
            return False
        self.reserved += ceiling
        return True

    def settle(self, ceiling: float, actual: float) -> None:
        self.reserved -= ceiling
        self.spent += actual


def _usage(calls: list[StageCall]) -> dict[str, int]:
    return {
        "input_tokens": sum(c.input_tokens for c in calls),
        "output_tokens": sum(c.output_tokens for c in calls),
        "cache_read_input_tokens": sum(c.cache_read_input_tokens for c in calls),
        "cache_creation_input_tokens": sum(c.cache_creation_input_tokens for c in calls),
    }


def _call_dict(c: StageCall) -> dict[str, Any]:
    return {
        "stage": c.stage,
        "model": c.model,
        "served_by": c.served_by,
        "input_tokens": c.input_tokens,
        "output_tokens": c.output_tokens,
        "cache_read_input_tokens": c.cache_read_input_tokens,
        "cache_creation_input_tokens": c.cache_creation_input_tokens,
        "cost_usd": c.cost_usd,
        "latency_ms": c.latency_ms,
        "stop_reason": c.stop_reason,
    }


def build_row(case: Case, rep: int, split: str, run: CaseRun, pipeline: str, latency_s: float) -> dict[str, Any]:
    g = run.guard
    plan = run.plan
    usage = _usage(run.calls)
    # The model the row is "about": the planner when planning ran, else the guard.
    headline = next((c for c in reversed(run.calls) if c.stage == "plan"), None) or next(
        (c for c in run.calls if c.stage.startswith("guard")), None
    )
    return {
        "prompt_id": case.id,
        "rep": rep,
        "prompt": case.intent,
        "tags": [case.category, case.language, case.source],
        "split": split,
        "status": "truncated" if plan is not None and plan.truncated else "ok",
        "stop_reason": next((c.stop_reason for c in reversed(run.calls) if c.stage == "plan"), None),
        "grade": grade(case, run),
        "model": (headline.served_by or headline.model) if headline else pipeline,
        "usage": usage,
        "cost_usd": run.cost_usd,
        "latency_s": round(latency_s, 3),
        "perf": {
            "cost_usd": run.cost_usd,
            "latency_s": round(latency_s, 3),
            "in_tokens": usage["input_tokens"],
            "out_tokens": usage["output_tokens"],
        },
        "meta": {
            "pipeline": pipeline,
            "expected": {
                "verdict": case.expected_verdict,
                "tier": case.expected_tier,
                "is_injection": case.is_injection,
                "category": case.category,
            },
            "observed": {
                "verdict": g.verdict,
                "tier": g.tier,
                "raw_tier": g.raw_tier,
                "reasons": list(g.reasons),
                "scores": g.scores,
            },
            "spec": run.spec,
            "plan_ran": plan is not None,
            "plan_refused": plan.refused if plan is not None else None,
            "plan": plan.raw if plan is not None else None,
            "offered": sorted(plan.offered) if plan is not None else None,
            "calls": [_call_dict(c) for c in run.calls],
            # A server-side fallback answered instead of the requested model:
            # counted in the summary, since such a row measures another model.
            "fallback_served": any(c.served_by and c.served_by != c.model for c in run.calls),
            "notes": case.notes,
        },
    }


def _done_keys(path: Path) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            keys.add((str(r["prompt_id"]), int(r.get("rep", 0))))
    return keys


def write_state(flow_dir: Path, cases: list[Case], splits: dict[str, str]) -> None:
    flow_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "metrics": METRICS,
        "perf_fields": PERF_FIELDS,
        "train_ids": sorted(c.id for c in cases if splits[c.id] == "train"),
        "test_ids": sorted(c.id for c in cases if splits[c.id] == "test"),
    }
    (flow_dir / "_state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


async def run_cases(cases: list[Case], pipeline: Pipeline, cfg: RunConfig) -> RunOutcome:
    if pipeline.live and cfg.max_usd is None:
        raise RunRefused("a live pipeline needs a spend cap (max_usd)")
    vdir = cfg.variant_dir
    (vdir / "traces").mkdir(parents=True, exist_ok=True)
    splits = assign_splits(cases)
    write_state(cfg.flow_dir, cases, splits)
    # How these rows were made, for the report. A resumed run must be the same
    # kind of run: mixing fake and live rows in one variant would average a
    # harness check into a model measurement.
    meta_path = vdir / "run.json"
    meta = {"pipeline": pipeline.name, "live": pipeline.live, "stages": cfg.stages}
    if meta_path.exists():
        before = json.loads(meta_path.read_text(encoding="utf-8"))
        if before != meta:
            raise RunRefused(f"{vdir} holds a {before} run; use another --variant for a {meta} run")
    meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    bind = getattr(pipeline, "bind", None)
    if callable(bind):
        bind(cases)

    results_path, errors_path = vdir / "results.jsonl", vdir / "errors.jsonl"
    done = _done_keys(results_path)
    outcome = RunOutcome()
    meter = _Meter(cfg.max_usd)
    write_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, cfg.concurrency))
    stop = asyncio.Event()

    async def append(path: Path, row: dict[str, Any]) -> None:
        async with write_lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    async def record_error(case: Case, rep: int, cls: str, detail: str, calls: list[StageCall]) -> None:
        outcome.errors += 1
        outcome.error_classes[cls] = outcome.error_classes.get(cls, 0) + 1
        await append(
            errors_path,
            {
                "prompt_id": case.id,
                "rep": rep,
                "failure_class": cls,
                "detail": detail[:500],
                "attempts": 1,
                "usage": _usage(calls),
                "cost_usd": sum(c.cost_usd for c in calls),
                "calls": [_call_dict(c) for c in calls],
            },
        )

    async def one(case: Case, rep: int) -> None:
        async with sem:
            if stop.is_set():
                outcome.skipped_budget += 1
                return
            ceiling = cost.reservation(case, stages=cfg.stages)
            if not meter.try_reserve(ceiling):
                outcome.skipped_budget += 1
                outcome.stopped = outcome.stopped or f"spend cap ${cfg.max_usd:.2f} reached"
                stop.set()
                return
            actual = 0.0
            started = time.perf_counter()
            try:
                run = await asyncio.wait_for(pipeline.run(case.intent, stages=cfg.stages), timeout=cfg.timeout_s)
            except TimeoutError:
                await record_error(case, rep, "timeout", f"no answer within {cfg.timeout_s:.0f}s", [])
            except PipelineUnavailable as e:
                actual = sum(c.cost_usd for c in e.calls)
                await record_error(case, rep, e.failure_class, e.detail, e.calls)
            except SpendCapReached as e:
                await record_error(case, rep, "spend_cap", str(e), [])
                outcome.stopped = "the app's daily spend cap stopped planning"
                stop.set()
            except Exception as e:  # the harness must not die on one case
                await record_error(case, rep, "harness_error", f"{type(e).__name__}: {e}", [])
            else:
                actual = run.cost_usd
                latency = time.perf_counter() - started
                row = build_row(case, rep, splits[case.id], run, pipeline.name, latency)
                trace = vdir / "traces" / f"{case.id}_rep{rep}.json"
                trace.write_text(json.dumps(run.transcript, ensure_ascii=False, indent=1), encoding="utf-8")
                await append(results_path, row)
                outcome.scored += 1
            finally:
                meter.settle(ceiling, actual)

    jobs = []
    for case in cases:
        for rep in range(cfg.reps):
            if (case.id, rep) in done:
                outcome.skipped_resume += 1
                continue
            jobs.append(one(case, rep))
    await asyncio.gather(*jobs)
    outcome.spent_usd = meter.spent
    return outcome


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
