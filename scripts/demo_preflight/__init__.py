"""The demo pre-flight (story 5.04): GO / NO-GO for recording the demo video.

    python -m scripts.demo_preflight --buyer G... --operator G... --cap 0.5 \\
        --out-dir docs/evidence/5.04/preflight

The video shows only real testnet transactions, so the live deployment has to
be in exactly the right state before the camera rolls: escrow v2 deployed and
settling, refunds switched on, the 5.02 routes deployed, a real external
operator bound and reachable, a real agent genuinely below the reputation
floor for the exclusion scene, and funded buyer and operator wallets. A take
that finds a missing piece halfway through wastes the session. This tool finds
it first, and every check says exactly what to fix.

It is read-only: every call is a GET or a simulation, retried a bounded number
of times, and it holds no secret. The one exception is opt-in —
`--with-decompose "<intent>"` POSTs one decompose (a model call and a stored
plan), once, never retried. It refuses any network but testnet, checked
against the RPC's `getNetwork`, Horizon's root document and the API's own
`/api/stellar/network`.

Exit codes (`config.py`): 0 GO; 3 refused (wrong network, bad flags, an
unreadable team register); 4 NO-GO, a required check FAILED; 5 NO-GO, nothing
failed but a required check was SKIPPED — never a pass.

Modules:

    config    exit codes, network constants, thresholds, run configuration
    retry     bounded retries, for reads only
    redact    the one output path; masks anything shaped like a secret
    api       the deployment's routes, the backend host's /readiness, the pages
    chain     read-only RPC (escrow views) and Horizon (accounts)
    register  the committed team register
    checks    every check, and the order they run in
    report    the verdict, and the checklist as terminal, Markdown and JSON
    cli       argparse and `main`
    fakes     an in-memory API + backend + RPC + Horizon + frontend for the tests

Structure and conventions follow `scripts/lifecycle/` and
`scripts/adoption_report/`; nothing is imported from either, or from `app/`,
at runtime. See docs/operators/demo-recording.md.
"""
