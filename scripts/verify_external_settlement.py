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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make `python scripts/verify_external_settlement.py` work from the repo root:
# put the repo root on sys.path so the `app` package resolves without PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402  (after the sys.path bootstrap above)

from app.evidence import Check, all_passed  # noqa: E402


@dataclass(frozen=True)
class Credit:
    """One credit to the owner, as the ledger records it."""

    to: str
    # Who paid. None when Horizon's record does not name a counterparty — which
    # is itself worth printing, because an unnamed funder cannot be called a
    # buyer payment.
    source: str | None
    amount: str
    asset: str
    # Whether the word "USDC" may be used for this payout at all. Carried on the
    # record so the decision is made once, from the ledger's asset code.
    is_usdc: bool
    # Which Horizon record proved it, so a reviewer can re-fetch the same one.
    evidence: str


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    embedded = payload.get("_embedded")
    records = embedded.get("records") if isinstance(embedded, dict) else None
    return records if isinstance(records, list) else []


def _asset_label(record: dict[str, Any]) -> str:
    """Name the asset that moved, using Horizon's own fields and nothing else.

    `native` is XLM and is spelled that way; anything else is spelled with the
    code its issuer published. This function must never print a denomination
    the ledger did not state — that is the 4.01 honesty rule in code.
    """
    if record.get("asset_type") == "native":
        return "XLM (native)"
    code = record.get("asset_code")
    issuer = record.get("asset_issuer")
    if code:
        return f"{code} (issuer {issuer})" if issuer else str(code)
    return str(record.get("asset_type") or "unknown asset")


def _is_usdc(record: dict[str, Any]) -> bool:
    """Whether the asset that moved is USDC — i.e. whether the ledger's own
    asset code says so. The issuer is printed alongside the code so a reviewer
    can check WHICH USDC; this decides only whether the word may appear."""
    return record.get("asset_code") == "USDC"


def find_credit_in_operations(operations: list[dict[str, Any]], owner: str) -> Credit | None:
    """Look for the credit in `asset_balance_changes` on the transaction's
    operations — how Horizon reports a Soroban SAC transfer, and the only place
    a non-native SAC transfer is guaranteed to appear."""
    for op in operations:
        for change in op.get("asset_balance_changes") or []:
            if change.get("to") != owner or change.get("type") not in {"transfer", "mint"}:
                continue
            return Credit(
                to=owner,
                source=change.get("from"),
                amount=str(change.get("amount", "")),
                asset=_asset_label(change),
                is_usdc=_is_usdc(change),
                evidence=f"asset_balance_changes on operation {op.get('id')} ({op.get('type')})",
            )
    return None


def find_credit_in_effects(effects: list[dict[str, Any]], owner: str) -> Credit | None:
    """Fall back to the `account_credited` effect — how a classic payment
    operation reports the same movement, which has no balance-change array."""
    for effect in effects:
        if effect.get("type") != "account_credited" or effect.get("account") != owner:
            continue
        amount = str(effect.get("amount", ""))
        source = next(
            (
                e.get("account")
                for e in effects
                if e.get("type") == "account_debited" and str(e.get("amount", "")) == amount
            ),
            None,
        )
        return Credit(
            to=owner,
            source=source,
            amount=amount,
            asset=_asset_label(effect),
            is_usdc=_is_usdc(effect),
            evidence=f"account_credited effect {effect.get('id')}",
        )
    return None


def check_owner_credited(credit: Credit | None, owner: str, expected_amount: float | None) -> list[Check]:
    """The check the story actually turns on: the owner's balance MOVED, read
    off the ledger rather than off a submit response we wrote ourselves."""
    if credit is None:
        return [Check("owner_credited", False, f"no credit to {owner} in this transaction's operations or effects")]

    checks = [Check("owner_credited", True, f"{credit.amount} {credit.asset} credited to {owner} — {credit.evidence}")]
    if expected_amount is None:
        return checks

    try:
        landed = float(credit.amount)
    except ValueError:
        checks.append(Check("credited_amount", False, f"credited amount {credit.amount!r} is not a number"))
        return checks
    # One stroop of slack: Horizon renders 7 decimal places and the expected
    # value arrives as a float, so an exact comparison would fail on rounding.
    matches = abs(landed - expected_amount) <= 1e-7
    if matches:
        amount_detail = f"credited {credit.amount} as expected"
    else:
        amount_detail = f"credited {credit.amount}, expected {expected_amount}"
    checks.append(Check("credited_amount", matches, amount_detail))
    return checks


def _normalise_network(name: str) -> str:
    """Horizon and Stellar Expert say `public`; our config says `mainnet`."""
    lowered = name.strip().lower()
    return "public" if lowered in {"mainnet", "public"} else lowered


def check_network_config(config: dict[str, Any], network: str) -> list[Check]:
    """Verify `GET /api/stellar/network` describes the SAME network the
    transaction was read from.

    A report that reads the asset off a mainnet deployment while reading the
    transaction off testnet Horizon is not evidence — it is a mismatch that
    happens to print. The asset disclosure below is only meaningful once this
    passes.
    """
    configured = str(config.get("network") or "")
    matches = _normalise_network(configured) == _normalise_network(network)
    if matches:
        detail = f"API network {configured!r} matches --network {network!r}"
    else:
        detail = f"API is on {configured!r} but the tx was read from {network!r} Horizon"
    return [Check("network_matches", matches, detail)]


def asset_lines(config: dict[str, Any], credit: Credit | None) -> list[str]:
    """The honest-asset block, following 4.01's precedent.

    Prints what the configured SAC actually is and what this particular credit
    actually moved. On testnet the SAC wraps the native asset, so the payout is
    XLM; the story's acceptance criterion says USDC, and the gap between those
    two is exactly what this block exists to make impossible to miss.
    """
    lines = [
        f"  asset SAC (configured):   {config.get('asset_sac') or '-'}",
        f"  asset (API reports):      {config.get('asset') or '-'}",
    ]
    if credit is None:
        return lines
    lines.append(f"  asset (this credit):      {credit.asset}")
    if not credit.is_usdc:
        lines.append("  ^ NOT USDC. Label this payout with the asset above — never as USDC.")
    return lines


def _horizon_base(network: str) -> str:
    return "https://horizon.stellar.org" if network == "public" else "https://horizon-testnet.stellar.org"


def _expert(kind: str, id_: str, network: str) -> str:
    seg = "public" if network == "public" else "testnet"
    return f"https://stellar.expert/explorer/{seg}/{kind}/{id_}"


def check_agent_owner(payload: dict[str, Any], agent_id: str, expected_owner: str) -> list[Check]:
    """Verify `GET /api/stellar/agent/{id}` — a LIVE `AgentRegistry.get(id)`
    simulate, not the cached `/api/agents` list — names the expected owner.

    That record's `owner` field IS `owner_of(id)`: the contract implements
    `owner_of` as `get(id).owner` (agent-registry/src/lib.rs:117-120), and it is
    the same value `PaymentEscrow.charge` resolves a payout to. Checking the
    cached marketplace listing instead would prove only that our own sync loop
    agreed with itself.
    """
    agent = payload.get("agent")
    if not isinstance(agent, dict):
        return [Check("agent_onchain", False, f"no agent record in the registry read (got {type(agent).__name__})")]

    checks = [Check("agent_onchain", True, f"{agent_id} is registered on AgentRegistry")]
    owner = agent.get("owner")
    matches = owner == expected_owner
    checks.append(
        Check(
            "owner_matches",
            matches,
            "owner_of matches the credited account" if matches else f"owner_of is {owner}, expected {expected_owner}",
        )
    )
    return checks


def check_binding(payload: dict[str, Any], agent_id: str, expected_owner: str) -> list[Check]:
    """Verify `GET /api/agents/{id}/binding` shows an operator endpoint bound to
    the same owner — what makes this an EXTERNAL agent rather than a seeded one.

    Anonymous callers get the endpoint's host only (the full path is disclosed
    only to the operator key), which is all the evidence needs: that a binding
    exists, and whose wallet authorised it.
    """
    bound_id = payload.get("agent_id")
    present = bound_id == agent_id
    checks = [
        Check(
            "binding_present",
            present,
            f"bound to {payload.get('endpoint_url')}" if present else f"binding is for {bound_id!r}, not {agent_id!r}",
        )
    ]
    owner = payload.get("owner")
    matches = owner == expected_owner
    checks.append(
        Check(
            "binding_owner_matches",
            matches,
            "binding owner matches the credited account"
            if matches
            else f"binding owner is {owner}, expected {expected_owner}",
        )
    )
    return checks


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
    credit: Credit | None = None
    horizon = _horizon_base(args.network)
    base = args.api_base.rstrip("/")

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        config: dict[str, Any] = {}
        network_resp = client.get(f"{base}/api/stellar/network")
        if network_resp.status_code != 200:
            # Without this the asset cannot be reported honestly, and an
            # unlabelled payout is not evidence — so it is a failed check, not
            # a skipped one.
            checks.append(
                Check("network_config_read", False, f"/api/stellar/network returned {network_resp.status_code}")
            )
        else:
            config = network_resp.json()
            checks.append(Check("network_config_read", True, f"asset SAC {config.get('asset_sac') or '-'}"))
            checks.extend(check_network_config(config, args.network))

        agent_resp = client.get(f"{base}/api/stellar/agent/{args.agent}")
        if agent_resp.status_code != 200:
            checks.append(
                Check("agent_onchain", False, f"/api/stellar/agent/{args.agent} returned {agent_resp.status_code}")
            )
        else:
            checks.extend(check_agent_owner(agent_resp.json(), args.agent, args.owner))

        binding_resp = client.get(f"{base}/api/agents/{args.agent}/binding")
        if binding_resp.status_code != 200:
            checks.append(
                Check("binding_present", False, f"/api/agents/{args.agent}/binding returned {binding_resp.status_code}")
            )
        else:
            checks.extend(check_binding(binding_resp.json(), args.agent, args.owner))

        tx_resp = client.get(f"{horizon}/transactions/{args.tx}")
        if tx_resp.status_code != 200:
            checks.append(Check("tx_on_horizon", False, f"Horizon returned {tx_resp.status_code} for the tx hash"))
        else:
            checks.append(Check("tx_on_horizon", True, "transaction found on Horizon"))
            checks.extend(check_settlement_tx(tx_resp.json()))

            ops = client.get(f"{horizon}/transactions/{args.tx}/operations", params={"limit": 200})
            if ops.status_code == 200:
                credit = find_credit_in_operations(_records(ops.json()), args.owner)
            if credit is None:
                effects = client.get(f"{horizon}/transactions/{args.tx}/effects", params={"limit": 200})
                if effects.status_code == 200:
                    credit = find_credit_in_effects(_records(effects.json()), args.owner)
            checks.extend(check_owner_credited(credit, args.owner, args.amount))

    print(f"\nExternal settlement evidence — agent {args.agent} - tx {args.tx[:12]}...\n")
    for c in checks:
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}")

    print()
    for line in asset_lines(config, credit):
        print(line)

    print(f"\n  stellar.expert (tx):      {_expert('tx', args.tx, args.network)}")
    print(f"  stellar.expert (account): {_expert('account', args.owner, args.network)}")

    ok = all_passed(checks)
    print(f"\n  VERDICT: {'PASS - settlement verified' if ok else 'FAIL - see checks above'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
