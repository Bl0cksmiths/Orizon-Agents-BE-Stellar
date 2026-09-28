"""The lifecycle harness (story 5.01): one buyer, one external agent, the whole chain.

    python -m scripts.lifecycle --api https://orizons.xyz --agent <id> \\
        --intent "<what to build>" --buyer-secret-env BUYER_1_SECRET \\
        --adjudicator-key-env ORIZON_API_KEY --evidence-dir docs/evidence/5.01/run-1

An operator tool, not a test. It drives the DEPLOYED API the way the dApp
does — the same routes, the same request bodies, the same headers — with a
buyer keypair standing in for the wallet and the operator key standing in for
the adjudicator, and it writes every transaction hash to an append-only
evidence file the moment the hash exists, read back from the ledger rather
than assumed. Story 5.05's evidence index is built from that file.

Nothing here imports `app/` at runtime. The harness talks to the backend over
HTTP only, exactly as a buyer would, so a drift between the two shows up as a
refusal from the server rather than as a harness that agrees with itself. The
places where the harness re-states a server-side format (the challenge
messages it checks before signing, the trace line it reads rating hashes
from, the stroop conversion) are pinned against `app/` by the test suite
instead — see `tests/test_lifecycle_contract.py`.

Modules:

    config    stages, exit codes, the testnet passphrase, run configuration
    redact    the one output path; masks every secret the run holds
    evidence  the append-only JSON Lines file, its Markdown rendering, run state
    api       the Orizon HTTP API, shaped exactly as `lib/api.ts` calls it
    chain     read-only Soroban RPC and Horizon: statuses, events, views
    signing   the buyer's key: authorize XDR and SEP-53 challenge signatures
    verify    pure settlement and seal checks for escrow v1 and v2
    stages    the nine stages, and the runner that sequences and resumes them
    cli       argparse and `main`
    fakes     an in-memory Orizon API + RPC + Horizon for the hermetic suite
"""
