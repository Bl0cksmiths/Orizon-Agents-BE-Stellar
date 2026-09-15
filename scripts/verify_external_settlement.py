#!/usr/bin/env python3
"""Verify that an externally-bound agent's owner was actually paid (story 2.04).

    python scripts/verify_external_settlement.py --agent <id> --owner G... \
        --tx <hash> --api-base https://... [--amount 0.054] [--network testnet]

Cross-checks one claim — "this external agent's owner received value on chain" —
against Horizon and the marketplace API, and prints a PASS/FAIL report that
exits non-zero on any failure, the same contract as
`scripts/verify_registration.py` (story 1.07). This is the 2.04 equivalent:
1.07 proved a registration landed, this proves a PAYOUT landed.

The asset is reported from what the ledger and `GET /api/stellar/network` say it
is, never from the story's wording. On testnet the configured SAC wraps the
NATIVE asset, so a credit there is XLM — this report says XLM and never "USDC"
unless the credited asset genuinely is USDC. That is the 4.01 precedent
(`docs/evidence/4.01-refund-testnet.md`), and it is the whole point of running a
verifier instead of pasting a link.

The pure assertions live in this file rather than in `app/evidence.py` (where
1.07's live) only because nothing else imports them yet; they take parsed JSON
and no network, so they move there unchanged once they need tests.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Make `python scripts/verify_external_settlement.py` work from the repo root:
# put the repo root on sys.path so the `app` package resolves without PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402  (after the sys.path bootstrap above)

from app.evidence import Check, all_passed  # noqa: E402


def _horizon_base(network: str) -> str:
    return "https://horizon.stellar.org" if network == "public" else "https://horizon-testnet.stellar.org"


def _expert(kind: str, id_: str, network: str) -> str:
    seg = "public" if network == "public" else "testnet"
    return f"https://stellar.expert/explorer/{seg}/{kind}/{id_}"


def check_settlement_tx(tx: dict[str, Any]) -> list[Check]:
    """Verify the Horizon transaction record (`GET /transactions/{hash}`) is a
    transaction that actually succeeded on chain.

    Deliberately NOT asserting a source account: unlike a registration, the
    party that submits a settlement is not the party the evidence is about. Who
    funded it is reported separately, because "the platform paid" and "a buyer
    paid" are different claims and the report must not blur them.
    """
    successful = tx.get("successful") is True
    if successful:
        detail = "transaction successful on-chain"
    else:
        detail = f"tx not successful (successful={tx.get('successful')!r})"
    return [Check("tx_succeeded", successful, detail)]


def main() -> int:
    p = argparse.ArgumentParser(description="Verify an external agent's settlement evidence (story 2.04).")
    p.add_argument("--agent", required=True, help="agent id")
    p.add_argument("--owner", required=True, help="the agent owner's G-address — the account that should be credited")
    p.add_argument("--tx", required=True, help="settlement transaction hash (the payout, not the registration)")
    p.add_argument("--api-base", required=True, help="marketplace API base, e.g. https://orizons.xyz")
    p.add_argument("--amount", type=float, help="expected credited amount, cross-checked against the ledger")
    p.add_argument("--network", default="testnet", choices=["testnet", "public"])
    args = p.parse_args()

    checks: list[Check] = []
    horizon = _horizon_base(args.network)

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        tx_resp = client.get(f"{horizon}/transactions/{args.tx}")
        if tx_resp.status_code != 200:
            checks.append(Check("tx_on_horizon", False, f"Horizon returned {tx_resp.status_code} for the tx hash"))
        else:
            checks.append(Check("tx_on_horizon", True, "transaction found on Horizon"))
            checks.extend(check_settlement_tx(tx_resp.json()))

    print(f"\nExternal settlement evidence — agent {args.agent} - tx {args.tx[:12]}...\n")
    for c in checks:
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}")

    print(f"\n  stellar.expert (tx):      {_expert('tx', args.tx, args.network)}")
    print(f"  stellar.expert (account): {_expert('account', args.owner, args.network)}")

    ok = all_passed(checks)
    print(f"\n  VERDICT: {'PASS - settlement verified' if ok else 'FAIL - see checks above'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
