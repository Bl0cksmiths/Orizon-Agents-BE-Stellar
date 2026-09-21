#!/usr/bin/env python3
"""Uphold one dispute, pay its credit, and print the on-chain evidence (story 4.03).

    python scripts/uphold_dispute.py --dispute-id dsp_1a2b3c4d5e6f7a8b

**This moves real funds on the configured network.** Story 4.03's first
acceptance criterion is a USDC transfer a reviewer can open on Stellar Expert,
and no CI job can produce one: it needs the funded settler key, which never
leaves an operator's hands. This script is that run, and it prints the hash and
the explorer URL that go into the Week-3 evidence bundle.

**The credit is funded by the PLATFORM, not clawed back from the agent.** The
deployed `PaymentEscrow` never takes custody — `charge` sends the buyer's funds
straight to the agent owner — so there is nothing to reverse. An upheld dispute
is a NEW transfer out of the settler's own wallet to the buyer. The disputed
agent's only consequence is reputational (story 4.04's rating), never a seizure
of its funds. Every artifact built from this output has to say so; that is ADR
0002's disclosed trust model, not a footnote.

Why this drives the service layer in-process, and not `POST /api/disputes/{id}/uphold`
-------------------------------------------------------------------------------------
Three reasons, in the order they matter:

1. **The dry run would be a lie over HTTP.** What a preview has to show — the
   settled step's price, the workflow's settled total, and which of D4's three
   bounds binds — lives in `SettlementRecord`, and no endpoint exposes it. A
   preview computed from different inputs than the live run uses is not a safety
   check, it is decoration. In-process both paths call one function,
   `refund_svc.creditable_for`, so the number the operator approves is the
   number that gets signed.
2. **The HTTP route adds nothing CI has not already proved.** All it contributes
   is `require_adjudicator` (D1), whose three refusals are unit-tested and need
   no funded key. What CI *cannot* prove is that a transfer lands, and that part
   is identical either way: one `claim_refund`, one SAC `transfer(settler →
   buyer)`, signed by the same key.
3. **It keeps the money route disarmed.** The HTTP path needs
   `DISPUTE_REFUNDS_ENABLED=true` — and therefore `API_KEY` — set on a
   deployment that is publicly reachable, and left set between evidence runs.
   In-process, the switch is on for the seconds this script runs, on the
   operator's own machine.

The evidence is not weaker for it. Point `DATABASE_URL` at the same Postgres the
deployment uses and the dispute's move to `credited`, carrying its `refund_tx`,
is served by that deployment's own `GET /api/disputes/{id}` the moment this
exits — so a reviewer still gets the API-visible half without the route ever
being armed.

What it needs in the environment
--------------------------------
`STELLAR_SIGNING_KEY` (the settler, funded via friendbot on testnet),
`STELLAR_ASSET_SAC`, `DISPUTE_REFUNDS_ENABLED=true`, and a `DATABASE_URL`
pointing at the store that actually holds the dispute. Turning that switch on
with a signing key and a SAC set makes `API_KEY` mandatory — the config refuses
to boot without it, on every network including testnet
(`config._money_capable_config_requires_api_key`).

No secret ever reaches the terminal: every line goes out through
`security.redact_secrets`, which masks this deployment's configured values and
anything secret-shaped from elsewhere. That is a property of the output path
rather than a promise about each line, so a line added later cannot leak a key
without going around `say`, and `tests/test_uphold_script.py` asserts none does.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

# Exit codes, so an operator — or the wrapper script somebody inevitably writes
# around this — can tell the refusals apart without parsing prose. 1 and 2 are
# left alone: 2 is argparse's usage error, and 1 is what an unhandled traceback
# exits with, so reusing either would blur a refusal into a crash.
EXIT_OK = 0
EXIT_NOT_CONFIGURED = 3
EXIT_UNKNOWN_DISPUTE = 4
EXIT_NOT_ADJUDICABLE = 5
EXIT_IN_FLIGHT = 6
EXIT_NOTHING_TO_CREDIT = 7
EXIT_ABOVE_CAP = 8
EXIT_TRANSFER_FAILED = 9
EXIT_TIMEOUT = 10
EXIT_UNEXPECTED = 11

# Make `python scripts/uphold_dispute.py` work from the repo root: put the repo
# root on sys.path so the `app` package resolves without PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402  (after the sys.path bootstrap above)

try:
    from app.config import settings  # noqa: E402
    from app.security import redact_secrets  # noqa: E402
    from app.services import refund_svc  # noqa: E402
    from app.services.dispute_store import (  # noqa: E402
        DisputeRecord,
        SettlementRecord,
        SettlementStep,
        get_dispute_store,
    )
except ValidationError:
    # The refund switch with no `API_KEY` behind it refuses the whole boot, and
    # that refusal arrives here, as an import error, before any of this file's
    # own output exists. Caught and answered in our own words rather than
    # re-raised, because pydantic renders `input_value` as a truncated repr of
    # the entire settings object — and the truncation keeps the TAIL of
    # `STELLAR_SIGNING_KEY`. A traceback that prints part of the settler's
    # secret is exactly what this script promises never to do.
    print(
        "\n  REFUSED (not_configured) — nothing was signed.\n"
        "  The service refused to start with this configuration. The usual cause on the\n"
        "  refund path: DISPUTE_REFUNDS_ENABLED is true and a signing key and asset SAC\n"
        "  are set, which makes API_KEY mandatory on every network. Set API_KEY, or clear\n"
        "  DISPUTE_REFUNDS_ENABLED to run read-only.\n"
        "  (The underlying error is withheld on purpose: pydantic prints a truncated repr\n"
        "  of the settings, which includes the tail of the signing key.)\n"
    )
    raise SystemExit(EXIT_NOT_CONFIGURED) from None


# `refund_svc.RefundRefused.code` is the service's stable vocabulary for a
# credit that must not be signed; this maps it onto the exit table above. An
# unmapped code falls to EXIT_UNEXPECTED rather than to any of the specific
# ones, so a refusal the service grows later cannot be mistaken for a refusal
# this script already understands.
_REFUSAL_EXITS = {
    "nothing_to_credit": EXIT_NOTHING_TO_CREDIT,
    "refund_above_cap": EXIT_ABOVE_CAP,
}


def say(line: str = "") -> None:
    """Print one line with every secret masked.

    Routing ALL output through one redacting helper is the difference between
    "this script does not print the signing key" as a claim and as a property:
    nothing can leak one without going around this function.
    """
    print(redact_secrets(line))


def expert_url(kind: str, identifier: str) -> str:
    """A Stellar Expert link on the network this process is configured for.

    Horizon and Stellar Expert say `public` where our config says `mainnet`, so
    the mapping is made here rather than assumed at each call site.
    """
    segment = "public" if settings.stellar_network.strip().lower() in {"mainnet", "public"} else "testnet"
    return f"https://stellar.expert/explorer/{segment}/{kind}/{identifier}"


def refuse(code: int, name: str, *lines: str) -> int:
    """Print a refusal that happened BEFORE anything was signed, and its code.

    Only for that case. A failure on the far side of a submission cannot claim
    nothing was signed, and says so in its own block instead.
    """
    say()
    say(f"  REFUSED ({name}) — nothing was signed.")
    for line in lines:
        say(f"  {line}")
    say()
    return code


async def resolve(dispute_id: str) -> tuple[DisputeRecord | None, SettlementRecord | None]:
    """The dispute and the settlement it is judged against, straight from the store.

    Both come from `get_dispute_store()` — the same store the API reads — so a
    preview run against a `DATABASE_URL` that points somewhere else finds
    nothing rather than quietly previewing a different deployment's dispute.
    """
    store = get_dispute_store()
    dispute = await store.get_dispute(dispute_id)
    if dispute is None:
        return None, None
    return dispute, await store.get_settlement(dispute.job_id_hex)


def describe(dispute: DisputeRecord, settlement: SettlementRecord, step: SettlementStep | None) -> None:
    """The facts the credit is computed from, printed before any of the arithmetic.

    The payer is named twice over — as the account and as an explorer link —
    because "who gets the money" is the one field a reviewer checks against the
    evidence bundle, and a G-address read off a terminal at 2am is the easiest
    thing in this output to get wrong.
    """
    say()
    say(f"  dispute:   {dispute.id}   status={dispute.status}")
    say(f"  job:       {dispute.job_id_hex}   task={dispute.task_id}")
    say(f"  opened:    {dispute.opened_at:.0f} (epoch)   reason={dispute.reason[:60]!r}")
    say(f"  payer:     {dispute.payer}")
    say(f"             {expert_url('account', dispute.payer)}")
    say("             ^ the account that gets credited — check this before a live run.")
    if step is None:
        say(f"  step:      {dispute.step_index} — NOT in settlement {settlement.job_id_hex}")
        return
    say(
        f"  step:      {step.step_index}  {step.agent_id}"
        f"  {(step.agent_name or '-')!r}  delivered={step.delivered}  price={step.price_usdc:.7f} USDC"
    )
    say(f"  settled:   {settlement.settled_usdc:.7f} USDC for the whole workflow, at {settlement.settled_at:.0f}")


def bounds(dispute: DisputeRecord, settlement: SettlementRecord, step: SettlementStep) -> list[tuple[float, str]]:
    """D4's three bounds, each with the thing it alone protects against.

    Listed rather than folded because an operator approving a payout needs to
    see WHICH record is holding the number down: a credit clamped by the settled
    total means the buyer was promised more than the chain ever took, and that
    is a fact about the settlement, not a rounding detail.

    The fraction is read from `settings` and not from `refund_svc`'s module
    default, because the deployment's configured share is what the live path
    applies; a preview using the default would agree with it only by accident.
    """
    return [
        (max(dispute.creditable_usdc, 0.0), "promised to the buyer when the dispute was opened"),
        (
            refund_svc.credited_amount_usdc(step.price_usdc, settings.dispute_credited_fraction),
            f"step {step.step_index} price x DISPUTE_CREDITED_FRACTION={settings.dispute_credited_fraction:g}",
        ),
        (settlement.settled_usdc, "ever settled on-chain for the whole workflow"),
    ]


def plan(dispute: DisputeRecord, settlement: SettlementRecord, step: SettlementStep | None) -> tuple[float | None, int]:
    """Print what would be paid and why, and return it alongside an exit code.

    `refund_svc.creditable_for` is the AUTHORITY for the number — the same call
    the live path makes — and the table above it only names the bounds that
    produced it. That split is the point: a preview that computed its own total
    would be a second implementation of D4, and two implementations of a money
    rule drift apart on the day it matters.

    Returns `(amount, EXIT_OK)` when there is a credit to pay, and
    `(None, <exit code>)` when `creditable_for` refuses. Every code it raises is
    raised before the amount reaches the settler's key, so the refusal goes out
    through `refuse`.
    """
    if step is not None:
        say()
        say("  D4 — the credit is the SMALLEST of three bounds:")
        bounded = bounds(dispute, settlement, step)
        smallest = min(value for value, _ in bounded)
        for value, why in bounded:
            say(f"    {value:.7f} USDC  {why}{'   <- BINDS' if value <= smallest else ''}")
        say(f"  D5 — cap on ONE credit: MAX_REFUND_USDC = {settings.max_refund_usdc:.7f} USDC")

    try:
        amount = refund_svc.creditable_for(settlement, dispute, settings.dispute_credited_fraction)
    except refund_svc.RefundRefused as exc:
        return None, refuse(_REFUSAL_EXITS.get(exc.code, EXIT_UNEXPECTED), exc.code, exc.message)

    say()
    say(f"  credit:    {amount:.7f} USDC  ->  {dispute.payer}")
    say(f"  funded by: the PLATFORM settler wallet — not clawed back from {dispute.agent_id}")
    return amount, EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """The CLI.

    The description is where an operator who ran `--help` instead of reading the
    module finds the two facts that change what they are about to do: that this
    spends real funds, and that the platform — not the disputed agent — is the
    one spending them.
    """
    return argparse.ArgumentParser(
        prog="uphold_dispute.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Uphold one dispute and print the evidence for its settler-funded credit (story 4.03).\n"
            "\n"
            "THIS MOVES REAL FUNDS on the network this process is configured for.\n"
            "\n"
            "The credit is FUNDED BY THE PLATFORM, not clawed back from the agent: the escrow\n"
            "never takes custody, so an upheld dispute is a new transfer out of the settler's\n"
            "own wallet to the buyer. The disputed agent's only consequence is reputational.\n"
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.add_argument(
        "--dispute-id",
        required=True,
        help="the dispute to uphold, as GET /api/disputes/{id} reports it",
    )
    args = parser.parse_args(argv)

    dispute, settlement = asyncio.run(resolve(args.dispute_id))
    if dispute is None:
        return refuse(
            EXIT_UNKNOWN_DISPUTE,
            "unknown_dispute",
            f"no dispute {args.dispute_id!r} in this store.",
            "Check the id, and check DATABASE_URL points at the store that holds it —",
            "an unset DATABASE_URL is an in-memory store that knows nothing.",
        )
    if settlement is None:
        return refuse(
            EXIT_NOTHING_TO_CREDIT,
            "nothing_to_credit",
            f"dispute {dispute.id} names job {dispute.job_id_hex}, which has no settlement record.",
            "Nothing can be computed without one: the credit is bounded by what actually settled.",
        )

    step = settlement.step(dispute.step_index)
    describe(dispute, settlement, step)
    amount, code = plan(dispute, settlement, step)
    if amount is None:
        return code
    say()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
