"""The adoption report (story 5.02): an independent verifier of SOW §6.3's three targets.

    python -m scripts.adoption_report --api https://orizons.xyz \\
        --registry C... --escrow C... \\
        --team-register app/data/team_wallets.json --out-dir docs/evidence/5.02

`GET /api/ecosystem/adoption` says which agents are externally operated, by
which wallets, and which of their workflows settled. This tool believes none
of it. It re-reads every claim from testnet itself — Horizon for accounts and
old transactions, Soroban RPC for `owner_of`, the registry record, the
settlement transaction and its `charged` events — recounts the three totals
from only what verified, and exits non-zero when a claim fails or the recount
disagrees with the API. The contract ids are flags, never taken from the API,
because "which escrow" is itself a claim.

It is read-only end to end: every call is a GET or a simulation, retried a
bounded number of times, and it holds no secret. It refuses any network but
testnet, checked against the RPC's `getNetwork`, Horizon's root document and
the API's own `network` field.

Exit codes (`config.py`): 0 verified; 3 refused; 4 the API did not answer in
the frozen shape; 5 a claim does not hold on the chain; 6 the API's totals or
MET flags disagree with the recount; 7 a target is NOT MET (only with
`--require-met`); 8 a chain read failed, so a claim is unverified.

Modules:

    config    targets, exit codes, network constants, run configuration
    retry     bounded retries, for reads (every call here is one)
    api       the endpoint, and its frozen shape
    register  the committed team register: every account it names
    chain     read-only RPC and Horizon: accounts, views, transactions, events
    verify    one check per claim, the recount, the comparison
    report    the Markdown evidence table and the JSON for the 5.05 index
    cli       argparse and `main`
    fakes     an in-memory API + RPC + Horizon for the hermetic suite

Structure and conventions follow `scripts/lifecycle/`; nothing is imported
from it, or from `app/`, at runtime.
"""
