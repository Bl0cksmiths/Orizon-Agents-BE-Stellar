"""`python -m evals.orchestrator` — see `evals/orchestrator/__init__.py`."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import cost, external, report
from .contract import Pipeline
from .dataset import Case, DatasetError, assign_splits, load
from .runner import RunConfig, RunRefused, run_cases
from .synthetic import NullPipeline, SyntheticPipeline

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3  # a live run the gate would not start
EXIT_STOPPED = 4  # the run stopped starting cases at a spend cap; resumable

DEFAULT_FLOW = Path(__file__).with_name("runs")
KEY_ENV = ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY")


def _select(cases: list[Case], args: argparse.Namespace) -> list[Case]:
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c.id in wanted]
    if args.categories:
        prefixes = tuple(args.categories.split(","))
        cases = [c for c in cases if c.category.startswith(prefixes)]
    if args.per_category:
        taken: Counter[str] = Counter()
        kept = []
        for c in cases:
            if taken[c.category] < args.per_category:
                taken[c.category] += 1
                kept.append(c)
        cases = kept
    return cases


def _cases(args: argparse.Namespace) -> list[Case]:
    cases = load()
    if args.external:
        cases += external.load(args.external.split(","))
    return _select(cases, args)


def _add_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stages", choices=("guard", "all"), default="guard", help="guard only, or guard+improve+plan")
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--ids", help="comma-separated case ids")
    p.add_argument("--categories", help="comma-separated category prefixes, e.g. legit_,injection_fence")
    p.add_argument("--per-category", type=int, help="at most N cases from each category (a stratified pilot)")
    p.add_argument("--external", help="comma-separated cached benchmarks to add: " + ",".join(external.BENCHMARKS))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m evals.orchestrator", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="check the dataset and print its composition")

    est = sub.add_parser("estimate", help="print the spend estimate for a selection; calls nothing")
    _add_selection(est)

    run = sub.add_parser("run", help="run the eval (fake unless --live)")
    _add_selection(run)
    run.add_argument("--pipeline", choices=("app", "oracle", "null", "synthetic"), default="app")
    run.add_argument("--live", action="store_true", help="call the real jev and Claude services (costs money)")
    run.add_argument("--max-usd", type=float, help="hard spend cap for a live run (required with --live)")
    run.add_argument("--flow-dir", type=Path, default=DEFAULT_FLOW)
    run.add_argument("--variant", default="baseline", help="baseline or v<N>")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--timeout-s", type=float, default=180.0, help="hard wall-clock ceiling per case")
    run.add_argument("--noise", type=float, default=0.15, help="label-score noise for the fake answers (not --live)")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument(
        "--force-fallback", action="store_true", help="fail every jev call so the Haiku fallback guard answers"
    )

    for name, helptext in (("report", "rewrite summary.md"), ("sweep", "rewrite sweep.md")):
        p = sub.add_parser(name, help=f"{helptext} from a variant directory's results")
        p.add_argument("variant_dir", type=Path)

    cmp = sub.add_parser("compare", help="planner composition before and after, from two variants' results")
    cmp.add_argument("before", type=Path)
    cmp.add_argument("after", type=Path)
    cmp.add_argument("--out", type=Path, help="also write the comparison here")

    wk = sub.add_parser("workers", help="run each built-in Claude worker once (live only)")
    wk.add_argument("--live", action="store_true", help="required: this calls the real Claude API")
    wk.add_argument("--max-usd", type=float, help="refused unless every worker's full budget fits under it")
    wk.add_argument("--out", type=Path, required=True)
    wk.add_argument(
        "--recheck",
        choices=("release", "code-length"),
        help="a re-check job set, run against --max-usd as a budget (streamed replies cut off at it)",
    )

    cp = sub.add_parser("campaign", help="build a live campaign's report and data files from its runs")
    cp.add_argument("--runs", type=Path, required=True)
    cp.add_argument("--out", type=Path, required=True)

    rc = sub.add_parser("recheck", help="build the release re-check report from its runs")
    rc.add_argument("--campaign", type=Path, required=True, help="the campaign's runs directory")
    rc.add_argument("--runs", type=Path, required=True)
    rc.add_argument("--out", type=Path, required=True)

    fx = sub.add_parser("fetch-external", help="download pinned public benchmarks into the git-ignored cache")
    fx.add_argument("keys", nargs="+", choices=sorted(external.BENCHMARKS))
    return ap


def _composition(cases: list[Case]) -> str:
    splits = assign_splits(cases)
    lines = [f"{len(cases)} cases"]
    for title, key in (
        ("verdict", lambda c: c.expected_verdict),
        ("tier", lambda c: c.expected_tier or "-"),
        ("category", lambda c: c.category),
        ("language", lambda c: c.language),
        ("split", lambda c: splits[c.id]),
    ):
        counts = Counter(key(c) for c in cases)
        lines.append(f"  {title}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return "\n".join(lines)


def _keys_present() -> list[str]:
    """Names of the missing keys (never their values)."""
    try:
        from app.config import settings

        values = {
            "ANTHROPIC_API_KEY": getattr(settings, "anthropic_api_key", None),
            "TYPESAFE_API_KEY": getattr(settings, "typesafe_api_key", None),
        }
    except Exception:
        values = {}
    missing = []
    for name in KEY_ENV:
        v: Any = values.get(name) or os.environ.get(name)
        secret = v.get_secret_value() if hasattr(v, "get_secret_value") else v
        if not secret:
            missing.append(name)
    return missing


def _pipeline(args: argparse.Namespace, cases: list[Case]) -> Pipeline:
    if args.pipeline == "oracle":
        return SyntheticPipeline(name="oracle", noise=0.0, seed=args.seed)
    if args.pipeline == "synthetic":
        return SyntheticPipeline(name="synthetic", noise=args.noise, seed=args.seed)
    if args.pipeline == "null":
        return NullPipeline()
    from .app_pipeline import AppPipeline  # needs app/llm and the guard + planner modules

    return AppPipeline.create(
        live=args.live,
        cases=cases,
        stages=args.stages,
        noise=args.noise,
        seed=args.seed,
        force_fallback=args.force_fallback,
    )


def _run(args: argparse.Namespace) -> int:
    cases = _cases(args)
    est = cost.estimate(cases, stages=args.stages, reps=args.reps, fallback=args.force_fallback)
    mode = "LIVE" if args.live else "fake (no paid calls)"
    print(f"[{mode}] estimate: {est.describe()}")
    if args.live:
        if args.pipeline != "app":
            print("refused: --live only applies to --pipeline app", file=sys.stderr)
            return EXIT_REFUSED
        if args.max_usd is None:
            print("refused: --live needs --max-usd N", file=sys.stderr)
            return EXIT_REFUSED
        if est.expected_usd > args.max_usd:
            print(
                f"refused: expected spend {cost.usd(est.expected_usd)} is over --max-usd {cost.usd(args.max_usd)}; "
                "narrow the selection (--per-category, --categories, --stages guard) or raise the cap",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        missing = _keys_present()
        if missing:
            print(f"refused: {', '.join(missing)} not set", file=sys.stderr)
            return EXIT_REFUSED
        print(f"spend cap: {cost.usd(args.max_usd)} measured (ceiling for this selection {cost.usd(est.ceiling_usd)})")
    try:
        pipeline = _pipeline(args, cases)
    except ImportError as e:
        print(
            f"the app pipeline cannot load ({e}): it needs app/llm and the guard, improver and planner modules. "
            "--pipeline oracle|synthetic|null run without them",
            file=sys.stderr,
        )
        return EXIT_USAGE
    cfg = RunConfig(
        flow_dir=args.flow_dir,
        variant=args.variant,
        reps=args.reps,
        stages=args.stages,
        concurrency=args.concurrency,
        timeout_s=args.timeout_s,
        max_usd=args.max_usd,
        guard_fallback=args.force_fallback,
    )
    outcome = asyncio.run(run_cases(cases, pipeline, cfg))
    summary, sweep_md = report.write(cfg.variant_dir)
    print(
        f"scored {outcome.scored}, failed {outcome.errors} {dict(outcome.error_classes) or ''}, "
        f"resumed past {outcome.skipped_resume}, not started {outcome.skipped_budget}; "
        + (f"measured spend {cost.usd(outcome.spent_usd)}" if pipeline.live else "nothing billed")
    )
    print(report.headline(cfg.variant_dir))
    print(f"summary: {summary}\nsweep:   {sweep_md}")
    if outcome.stopped:
        print(f"stopped early: {outcome.stopped} (re-run the same command to resume)", file=sys.stderr)
        return EXIT_STOPPED
    return EXIT_OK


def _workers(args: argparse.Namespace) -> int:
    from .workers_sample import CODE_LENGTH_JOBS, HEADROOM, JOBS, RECHECK_JOBS, ceiling_usd

    if not args.live or args.max_usd is None:
        print("refused: the worker sample needs --live and --max-usd N", file=sys.stderr)
        return EXIT_REFUSED
    jobs = {"release": RECHECK_JOBS, "code-length": CODE_LENGTH_JOBS}.get(args.recheck or "", JOBS)
    if args.recheck:
        typical = sum(j.typical_usd for j in jobs)
        print(f"[LIVE] worker re-check: {len(jobs)} jobs, last measured {cost.usd(typical)}")
        # Each job starts only if it still fits, so the gate is the first one.
        if jobs[0].typical_usd * HEADROOM > args.max_usd:
            print("refused: the first job's measured cost with its headroom is over --max-usd", file=sys.stderr)
            return EXIT_REFUSED
    else:
        ceiling = ceiling_usd()
        print(f"[LIVE] worker sample: 7 workers, ceiling {cost.usd(ceiling)}")
        if ceiling > args.max_usd:
            print(f"refused: ceiling {cost.usd(ceiling)} is over --max-usd {cost.usd(args.max_usd)}", file=sys.stderr)
            return EXIT_REFUSED
    missing = [k for k in _keys_present() if k == "ANTHROPIC_API_KEY"]
    if missing:
        print("refused: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return EXIT_REFUSED
    from .workers_sample import run_sample

    results = asyncio.run(run_sample(args.out, jobs, args.max_usd if args.recheck else None))
    total = sum(r.get("cost_usd", 0.0) for r in results.values())
    for name, r in results.items():
        if "skipped" in r:
            print(f"{name}: skipped ({r['skipped']})")
            continue
        status = r["error"] or "ok"
        print(f"{name}: {r['latency_s']}s {cost.usd(r['cost_usd'])} {status}")
    print(f"total {cost.usd(total)}; outputs in {args.out}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "validate":
            print(_composition(load()))
            return EXIT_OK
        if args.cmd == "estimate":
            print(cost.estimate(_cases(args), stages=args.stages, reps=args.reps).describe())
            return EXIT_OK
        if args.cmd == "run":
            return _run(args)
        if args.cmd in ("report", "sweep"):
            summary, sweep_md = report.write(args.variant_dir)
            print(summary if args.cmd == "report" else sweep_md)
            return EXIT_OK
        if args.cmd == "compare":
            text = report.compare(args.before, args.after)
            if args.out:
                args.out.write_text(text, encoding="utf-8")
            print(text)
            return EXIT_OK
        if args.cmd == "campaign":
            from .campaign import build

            analysis = build(args.runs, args.out)
            print(f"report: {args.out / 'REPORT.md'}; campaign spend {cost.usd(analysis['grand_total_usd'])}")
            return EXIT_OK
        if args.cmd == "recheck":
            from .recheck import build as build_recheck

            build_recheck(args.campaign, args.runs, args.out)
            print(f"report: {args.out / 'REPORT.md'}")
            return EXIT_OK
        if args.cmd == "workers":
            return _workers(args)
        if args.cmd == "fetch-external":
            for key in args.keys:
                r = external.fetch(key)
                print(
                    f"{r.benchmark}@{r.revision[:12]} ({r.license}): kept {r.kept}, "
                    f"skipped {r.skipped_length} out of length bounds, {r.skipped_duplicate} duplicates"
                )
            return EXIT_OK
    except DatasetError as e:
        print(f"dataset error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RunRefused as e:
        print(f"refused: {e}", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_USAGE
