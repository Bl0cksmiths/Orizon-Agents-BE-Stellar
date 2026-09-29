"""The demo evidence sheet (story 5.04): the video's transactions, re-verified.

    python -m scripts.demo_evidence docs/evidence/5.04/take-1 docs/evidence/5.04/take-2 \\
        --out-dir docs/evidence/5.04/video

Input: the lifecycle harness's `lifecycle.jsonl` from each recording run (or
the evidence directories that hold them). Output, in `--out-dir`:

    evidence-sheet.md   every transaction the video shows, in order: stage,
                        what it proves, its D1–D4 deliverable, the hash, its
                        Stellar Expert testnet link, the status the harness
                        recorded and the status re-read just now
    description.txt     a ready-to-paste video description: a chapters
                        placeholder, every verified hash with its link, and
                        the limitations paragraph
    evidence.json       the list the frontend's /demo page reads (frozen shape,
                        see `render.py`)

Every hash is re-verified read-only — Soroban RPC `getTransaction`, falling
back to Horizon — and one that is not SUCCESS is listed in the sheet as failed
and NEVER written to `evidence.json` or the description. A row from any
network but testnet is refused before a hash is read, and so is an RPC or
Horizon that is not on testnet.

Exit codes (`config.py`): 0 every hash SUCCESS; 3 refused (nothing written);
5 a hash is FAILED, NOT_FOUND or malformed (outputs written without it); 8 a
read failed, so a hash is unverified (outputs written without it; rerun).

Modules:

    config    exit codes, network constants, the kind and deliverable maps
    retry     bounded retries, for reads (every call here is one)
    redact    masks anything shaped like a secret in every output
    rows      the harness's evidence rows, and the network refusal
    chain     one hash re-verified: RPC, then Horizon
    render    the sheet, the description and the frozen JSON
    cli       argparse and `main`
    fakes     an in-memory RPC + Horizon for the hermetic suite

Nothing is imported from `scripts/lifecycle/` or `app/` at runtime; the row
shape it reads is pinned against the harness by the test suite. See
docs/operators/demo-recording.md.
"""
