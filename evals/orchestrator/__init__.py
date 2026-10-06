"""Evals for the Claude + jev orchestrator: the guard, the improver and the planner.

    python -m evals.orchestrator validate
    python -m evals.orchestrator estimate --stages all
    python -m evals.orchestrator run --pipeline oracle            # harness check, free
    python -m evals.orchestrator run                              # real code on FakeJev/FakeClaude, free
    python -m evals.orchestrator run --live --max-usd 1 --stages guard   # the owner's call; costs money

What it measures, per labelled intent (`dataset.jsonl`, 168 cases):

* the guard's verdict (allow / block / needs_detail) and routed tier;
* with `--stages all`, the planner's RAW plan (before the allowlist clamp):
  schema-valid, only offered agents, every step tier present.

What it reports (`runs/<variant>/summary.md`): injection recall and precision,
block precision, false-block rate (overall and on borderline-but-legitimate
security asks), needs-detail recall, tier accuracy with a confusion matrix,
plan validity, accuracy by category / language / split — each with a Wilson
95% interval and its n — plus measured spend. `sweep.md` replays the guard's
rule over the recorded jev scores at other thresholds, picking on the train
split and judging on the held-out split.

Money: nothing is paid for unless `--live` is given, and `--live` needs
`--max-usd`. The estimate is printed before anything runs, a live run whose
estimate exceeds the cap is refused, and the runner stops starting cases once
measured spend plus the next case's ceiling would pass it.

Modules:

    dataset     the labelled set, its labelling policy, the stratified split
    external    pinned public benchmarks fetched into a git-ignored cache
    contract    what a pipeline reports; the Pipeline protocol
    policy      the guard's decision rule as replayable data
    synthetic   oracle / null / noisy pipelines — the harness's known answers
    app_pipeline  the real guard, improver and planner, over fakes or live
    grading     per-case programmatic checks
    metrics     aggregates with Wilson intervals
    sweep       the threshold sweep
    cost        the pre-run spend estimate
    runner      concurrent, resumable runs; results / errors / traces
    report      summary.md and sweep.md from the files on disk
    cli         argparse and `main`
"""
