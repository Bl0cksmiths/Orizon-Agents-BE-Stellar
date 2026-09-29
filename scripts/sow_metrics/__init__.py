"""The SOW §6.3 success metrics, measured (story 5.05, BLO-39).

    python -m scripts.sow_metrics --out-dir docs/evidence/5.05/metrics

The evidence index states each of the eleven SOW §6.3 metrics with its target,
the value achieved, how it was measured and links to the proof, with every
missed target stated plainly. This tool measures those values from Stellar
testnet and the live deployment, the same way every time, so nobody types a
number in by hand. It writes three files: the frozen metrics block the
frontend's `content/evidence/index.json` pastes (`sow-metrics.block.json`), a
Markdown table (`sow-metrics.md`), and the raw counted and excluded items per
metric (`sow-metrics.raw.json`).

A milestone page that answers 404 is never linked: a dead link proves
nothing. `--pending-link ID=URL[=label]` links the pull request that adds it
instead, and without one the row links nothing and its method says why.

No outside operator's agent id, wallet or hash is published before their
consent is recorded: by default (`--withhold-external`) every proof link that
names one is replaced by one link to the Ecosystem page, and the counts do
not change. `--publish-external` links them once consent exists. The raw
JSON always keeps them.

It is read-only: every call is a GET or a simulation, retried a bounded number
of times, and it holds no secret. It refuses any network but testnet, checked
against the RPC's `getNetwork`, Horizon's root document and the API's own
`/api/stellar/network`.

Exit codes (`config.py`): 0 measured, whatever was met or not met; 3 refused
(not testnet, bad flags, an unreadable team register); 4 a read failed, so at
least one metric is "Not measured" — reported as not met, never as 0.

Modules:

    config    exit codes, network constants, the SOW rows verbatim, run configuration
    retry     bounded retries, for reads only
    register  the committed team register
    api       the deployment's routes, the backend host, the frontend pages, GitHub
    chain     read-only RPC (contract state and views) and Horizon (account histories)
    collect   every read, into one snapshot, with per-source failures
    metrics   the rules: pure functions of the snapshot
    report    the frozen block and its shape check, the Markdown, the raw JSON
    cli       argparse and `main`
    fakes     an in-memory chain + deployment + GitHub for the tests

Structure and conventions follow `scripts/demo_preflight/` and
`scripts/adoption_report/`; nothing is imported from either, or from `app/`,
at runtime. See docs/operators/sow-metrics.md.
"""
