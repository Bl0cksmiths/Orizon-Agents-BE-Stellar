#!/usr/bin/env python3
"""Uphold one dispute, pay its credit, rate the agent, and print the on-chain evidence (4.03, 4.04).

    # ALWAYS FIRST — resolves everything, signs nothing, needs no signing key:
    python scripts/uphold_dispute.py --dispute-id dsp_1a2b3c4d5e6f7a8b --dry-run

    # then, once the dry run reads right:
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

**One live run produces both halves of the dispute's evidence.** The credit,
and — once it has landed and is recorded — the agent's `dispute`-kind rating on
the ReputationLedger (4.04): two transactions, printed side by side with their
Stellar Expert links, beside the agent's dispute rate as it stood before the
run and after it. The dry run previews both. For the rating that means its
value, its weight and the DERIVED id it is filed under, stacked over the sealed
job id so the 8 bytes the two share — the link a reviewer follows from the
rating back to the job — are plain to see.

**Whether to re-run is in the exit code, and it is not always "never".** A
timed-out CREDIT must not be retried (10): it may still land, and a second
transfer pays the buyer twice. A rating that did not land after a credit that
did (12) is the opposite case: `uphold` never pays a credited dispute again,
and the ledger refuses a second rating under the same id, so a re-run retries
the rating and nothing else. A rating collision (13) is neither, and is looked
up by hand before anything else.

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
   buyer)`, signed by the same key. `DISPUTE_REFUNDS_ENABLED` is not skipped
   either — `dispute_svc.uphold` checks the switch itself, before it reads the
   store, precisely because an operator script is the second door onto the money
   path and a switch only one door honours is not a switch.
3. **It keeps the money route disarmed.** The HTTP path needs
   `DISPUTE_REFUNDS_ENABLED=true` — and therefore `API_KEY` — set on a
   deployment that is publicly reachable, and left set between evidence runs.
   In-process, the switch is on for the seconds this script runs, on the
   operator's own machine.

The evidence is not weaker for it. Point `DATABASE_URL` at the same Postgres the
deployment uses and the dispute's move to `credited`, carrying its `refund_tx`
and its `rating_tx`, is served by that deployment's own `GET /api/disputes/{id}`
the moment this exits — so a reviewer still gets the API-visible half without
the route ever being armed.

What it needs in the environment
--------------------------------
`STELLAR_SIGNING_KEY` (the settler, funded via friendbot on testnet),
`STELLAR_ASSET_SAC`, `STELLAR_REPUTATION_LEDGER` with `REPUTATION_ENABLED` left
on (and the settler registered as the ledger's Scorer, or every rating it signs
is refused), `DISPUTE_REFUNDS_ENABLED=true`, and a `DATABASE_URL` pointing at
the store that actually holds the dispute. Turning that switch on with a
signing key and a SAC set makes `API_KEY` mandatory — the config refuses to
boot without it, on every network including testnet
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
import logging
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, TextIO

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
# Story 4.04's two, and both mean THE BUYER HAS BEEN PAID: the credit landed and
# is recorded, and only the agent's dispute rating is wrong. They are new codes
# rather than 9 or 10 because they ask for the opposite of what those ask for.
# 12 is the one outcome after a signature where re-running is RIGHT — a re-run
# retries the rating alone, and the ledger's replay guard means it cannot land
# twice. 13 is a collision, which no re-run can fix and a human must look up.
EXIT_RATING_NOT_LANDED = 12
EXIT_RATING_COLLISION = 13

# Make `python scripts/uphold_dispute.py` work from the repo root: put the repo
# root on sys.path so the `app` package resolves without PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402  (after the sys.path bootstrap above)

try:
    from app.config import settings  # noqa: E402
    from app.security import SecretRedactionLogFilter, redact_secrets  # noqa: E402
    from app.services import dispute_rating, dispute_svc, rating_writer, refund_svc, reputation_svc  # noqa: E402
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


# The services' stable refusal vocabulary, mapped onto the exit table above.
# Both halves of it: the two codes `refund_svc.RefundRefused` raises straight
# out of `creditable_for` during the preview, and the ones `dispute_svc.uphold`
# answers a live run with — which include those same two, because `uphold`
# catches them and re-raises them as `DisputeError` with the code intact.
#
# `refund_unconfirmed` is the timeout, and `report` overrides it from the record
# anyway; it is mapped here so that a run whose store read then fails still
# exits on the timeout code rather than on a generic one.
#
# An unmapped code deliberately does NOT fall to the nearest neighbour: a
# refusal either service grows later must not be mistaken for one this script
# already understands.
_REFUSAL_EXITS = {
    "nothing_to_credit": EXIT_NOTHING_TO_CREDIT,
    "settlement_missing": EXIT_NOTHING_TO_CREDIT,
    "refund_above_cap": EXIT_ABOVE_CAP,
    "refunds_disabled": EXIT_NOT_CONFIGURED,
    "unknown_dispute": EXIT_UNKNOWN_DISPUTE,
    "dispute_rejected": EXIT_NOT_ADJUDICABLE,
    "refund_in_flight": EXIT_IN_FLIGHT,
    "refund_failed": EXIT_TRANSFER_FAILED,
    "refund_unconfirmed": EXIT_TIMEOUT,
}

# The codes above that are raised on the FAR SIDE of a submission: something was
# signed, whatever became of it. They must never go out through `refuse`, whose
# whole message is that nothing was — on a money path that sentence is the one
# an operator acts on, and being wrong about it is how a timed-out credit gets
# retried. The report block reads the record and says what actually happened.
_POST_SIGNING_CODES = frozenset({"refund_failed", "refund_unconfirmed", "refund_in_flight"})

# The script's own log lines — only the ones no service writes, like a failed
# reputation read — go through the same redacted stderr handler as theirs.
logger = logging.getLogger("uphold_dispute")


def install_logging() -> None:
    """Send the service's own log lines to stderr, redacted.

    Half of what happened on a money path is in those lines and nowhere else:
    which D4 bound clamped the credit and by how much, the ERROR carrying
    dispute id, job, payer and amount that a refusal writes, and the
    reconciliation line D3 writes on a timeout. Without a handler, Python drops
    everything below WARNING and prints the rest bare.

    INFO, because the dispute store announces at INFO which store it resolved —
    the first thing to check when a dispute id "does not exist" and the answer
    is an unset DATABASE_URL.

    stderr, so the evidence block on stdout stays clean enough to paste into the
    bundle. The filter is the same one `app.main` installs, attached here rather
    than inherited because importing `app.main` would build the entire
    application just to borrow a log handler.

    Called from `__main__` only, never from `main()`: `basicConfig(force=True)`
    tears out every root handler, and doing that to a test session would take
    pytest's log capture with it.
    """
    logging.basicConfig(level=logging.INFO, handlers=[redacted_handler(sys.stderr)], force=True)


def redacted_handler(stream: TextIO) -> logging.Handler:
    """The handler `install_logging` puts on stderr: formatted, and masked first.

    Its own function so a test can attach the very handler an operator's
    terminal gets — to a buffer, beside pytest's capture rather than in place
    of it — and check what reaches stderr instead of trusting it. That matters
    most once a rating fails after the buyer is paid: the service logs the
    failure with its traceback rather than raising, so stderr is where a key
    quoted back in a signer's error would surface.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("  [%(levelname)s] %(name)s: %(message)s"))
    handler.addFilter(SecretRedactionLogFilter())
    return handler


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


def unresolved_credit(dispute: DisputeRecord, code: int, headline: str, amount: float | None = None) -> int:
    """The block for a dispute sitting in `crediting` — a transfer MAY BE IN FLIGHT.

    Reached two ways: a run that found the dispute already parked there, and a
    run whose own submission timed out. They are the same situation, so they get
    the same words rather than two half-written versions of them.

    Nothing here tidies up, and that is D3 rather than an unfinished path. A
    timed-out submission may still settle, so releasing the claim or retrying
    would credit the buyer twice the moment it does. A buyer credited late is
    recoverable; a buyer credited twice is not, and the settler's wallet is the
    platform's own money.
    """
    # The promise frozen at opening time is the best figure available when this
    # is reached before the credit has been computed; a caller that knows the
    # real one passes it.
    amount = dispute.creditable_usdc if amount is None else amount
    say()
    say("  " + "#" * 74)
    say(f"  #  {headline}")
    say("  #  DO NOT RE-RUN THIS SCRIPT FOR THIS DISPUTE.")
    say("  " + "#" * 74)
    say()
    say(f"  Dispute {dispute.id} is parked in `crediting`, which means the refund claim is")
    say("  STILL HELD. That is what stops anything — this script, the API, a retry — paying")
    say("  it a second time, and it is deliberate (D3), not a stuck job.")
    say()
    if dispute.refund_tx:
        say(f"  in-flight tx:  {dispute.refund_tx}")
        say(f"  check it:      {expert_url('tx', dispute.refund_tx)}")
    else:
        say("  in-flight tx:  none recorded — the submission returned no hash at all.")
    say(f"  the payer:     {expert_url('account', dispute.payer)}")
    say(f"                 look for a credit of about {amount:.7f} USDC to this account.")
    say()
    say("  Every claim held this way is listed by the store's `list_refund_claims()`, so a")
    say("  dispute stuck here is findable later without this terminal.")
    say()
    say("  Reconcile it on-chain first, then:")
    say("    * it SUCCEEDED — the buyer has been credited. Close the dispute by recording")
    say(f"      what landed: append_status({dispute.id!r}, 'credited', refund_tx=<hash>).")
    say("      Re-running this script instead would credit them a second time.")
    say("    * it FAILED, or the hash is on no explorer and the payer's balance never moved —")
    say(f"      nothing moved. release_refund_claim({dispute.id!r}) returns it to `upheld`,")
    say("      and only then may this script be run again.")
    say("    * you cannot tell — LEAVE IT. Late is recoverable. Twice is not.")
    say()
    return code


# The two states a credit may legitimately be paid from. `open` is the ordinary
# case. `upheld` is the resumable one: an earlier run (or an adjudicator over
# the API) recorded the decision but the credit was never claimed, and
# `claim_refund` starts from exactly there.
_ADJUDICABLE = ("open", "upheld")


def check_status(dispute: DisputeRecord) -> int:
    """EXIT_OK while this dispute may still be paid or rated; a refusal code otherwise.

    `claim_refund` is the real lock and refuses every one of these on its own
    (D2). This gate exists so the operator gets a sentence rather than a silent
    no-op — and so a preview says the same thing a live run would, which is the
    only reason to trust a preview at all.

    A `credited` dispute is let through since 4.04, to its RATING ONLY. That is
    `uphold`'s own rule (D3): on a credited dispute it signs no transfer and
    re-attempts the rating, which the ledger's replay guard makes safe however
    often it runs. So this is how a rating that did not land is retried, and
    how one that did is re-confirmed — and the operator is told, before
    anything runs, that the credit will not be paid again. Without a refund
    hash on record it is still refused: that dispute needs reconciling before
    anything is written against it, its rating included.
    """
    if dispute.status == "credited":
        if not dispute.refund_tx:
            return refuse(
                EXIT_NOT_ADJUDICABLE,
                "already_credited",
                f"dispute {dispute.id} is credited, but no refund tx is recorded against it.",
                "Reconcile the payer's account on-chain before anything else — its rating included.",
            )
        say()
        say("  note: ALREADY CREDITED — the credit will NOT be paid again. Its evidence:")
        say(f"        refund tx:  {dispute.refund_tx}")
        say(f"        evidence:   {expert_url('tx', dispute.refund_tx)}")
        if dispute.rating_tx:
            # Not "rated": the service records an in-flight hash on a rating
            # timeout exactly as it records a landed one, so only the ledger's
            # answer to a live run can say which this is.
            say(f"        rating tx:  {dispute.rating_tx}")
            say("                    on record, NOT confirmed — landed, or timed out in flight;")
            say("                    a live run asks the ledger which")
        else:
            say("        rating tx:  none on record — its dispute rating has not landed")
        say("        A live run writes, or re-confirms, the dispute rating ONLY: uphold signs no")
        say("        second transfer for a credited dispute.")
        return EXIT_OK
    if dispute.status == "rejected":
        return refuse(
            EXIT_NOT_ADJUDICABLE,
            "already_rejected",
            f"dispute {dispute.id} was rejected. That is final — adjudication is not re-run from here.",
        )
    if dispute.status == "crediting":
        return unresolved_credit(dispute, EXIT_IN_FLIGHT, "A CREDIT FOR THIS DISPUTE IS ALREADY IN FLIGHT.")
    if dispute.status not in _ADJUDICABLE:
        # Not reachable through `DisputeStatus` today. Kept because the status
        # is read back out of a database, and a money path that assumes its
        # inputs are well-formed is one schema change away from paying on a
        # value nobody considered.
        return refuse(
            EXIT_NOT_ADJUDICABLE,
            "not_adjudicable",
            f"dispute {dispute.id} is {dispute.status!r}, which is not a state a credit is paid from.",
        )
    if dispute.status == "upheld":
        say()
        say("  note: already upheld by an earlier run — the decision stands and the credit was")
        say("        never claimed, so this run pays it rather than adjudicating again.")
    return EXIT_OK


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


# How much of the rating id is the sealed job id's own, in the hex characters an
# operator reads: the derivation keeps the job's first 8 bytes verbatim (4.04
# D1), and the preview underlines exactly that span — not whatever longer run
# the hash half happens to share by chance.
_SHARED_PREFIX_HEX = 16


def preview_rating(dispute: DisputeRecord, step: SettlementStep | None) -> bool:
    """Print the dispute rating the live run writes once the credit lands (4.04).

    Every value the ReputationLedger will be handed, from the same calls the
    live path makes — `dispute_rating.dispute_job_id` for the id and
    `reputation_svc.rating_weight_stroops` for the weight (D2) — for `plan`'s
    reason: a preview that did its own arithmetic would be decoration.

    The two ids are printed whole and stacked, with the shared prefix
    underlined. Whole, so either pastes straight into Stellar Expert's search;
    stacked, because that prefix is the entire reason the derivation was
    chosen — it is how a grant reviewer who opens the rating transaction ties
    it to the job it disputes without reading this code.

    False when no rating can be written for this dispute, which the caller
    refuses before anything is signed: paying a credit whose reputation
    consequence is already known to be impossible leaves a dispute that can
    never be fully resolved, and a buyer paid late is recoverable where that
    is not.
    """
    say()
    say("  4.04 — the agent's consequence: once the credit lands, the settler writes this rating")
    if step is None:
        say(f"    step {dispute.step_index} is not in the settlement, so there is no price to weight it by.")
        return False
    try:
        job_id = bytes.fromhex(dispute.job_id_hex)
        rating_id = dispute_rating.dispute_job_id(job_id, dispute.step_index)
    except ValueError as exc:
        say(f"    no rating id can be derived for job {dispute.job_id_hex!r}: {exc}")
        return False
    weight = reputation_svc.rating_weight_stroops(step.price_usdc)
    say(f"    agent:     {dispute.agent_id}")
    say(f'    rating:    {dispute_rating.DISPUTE_RATING} / 100   kind = "dispute"')
    say(
        f"    weight:    {weight} stroops = {weight / reputation_svc.STROOPS_PER_USDC:.7f} USDC"
        f"  (step {step.step_index}'s quoted price, as every rating is weighted)"
    )
    say(f"    job id:    {job_id.hex()}   sealed — the job the settlement attested")
    say(f"    rating id: {rating_id.hex()}   derived — the key the rating is filed under")
    underline = "^" * _SHARED_PREFIX_HEX
    say(f"               {underline} the job's own 8 bytes: they tie this rating to it on Stellar Expert")
    return True


def check_config() -> int:
    """EXIT_OK when this process could sign a credit and write its rating; a refusal otherwise.

    Checked before the uphold call rather than left to the service, so an
    operator who forgot one environment variable learns it from a sentence
    instead of from a stack trace out of the signing path — and learns it
    BEFORE a dispute has been moved to `upheld` by a run that then cannot pay.

    Presence only. No value is printed and the signing key is not even read:
    that a secret is set is the whole of what this needs to know.

    The rating is checked too, because it is half of what a live run is for
    (4.04): a deployment that cannot write one would land the credit, skip the
    rating, and end with the buyer paid and the dispute unresolved until
    somebody noticed. The check is `rating_writer.config_gap` — the very gate
    `uphold` applies before it submits a rating — so this refuses exactly the
    runs the service would pay and then decline to rate, and names the same
    setting the service's own log line would.
    """
    missing = [
        name
        for name, present in (
            ("DISPUTE_REFUNDS_ENABLED=true", settings.dispute_refunds_enabled),
            ("STELLAR_SIGNING_KEY (the funded settler)", bool(settings.stellar_signing_key.strip())),
            ("STELLAR_ASSET_SAC", bool(settings.stellar_asset_sac.strip())),
        )
        if not present
    ]
    gap = rating_writer.config_gap()
    if gap is not None and gap.status != "no_signer":
        # `no_signer` is the signing key, already named above in the words
        # this script uses for it.
        missing.append(f"{gap.problem} — so the dispute rating could not be written")
    if not missing:
        return EXIT_OK
    return refuse(
        EXIT_NOT_CONFIGURED,
        "not_configured",
        "this process cannot sign a credit and write its rating. Missing:",
        *(f"    - {name}" for name in missing),
        "Set them, re-run with --dry-run, and only then live. Turning the switch on beside a",
        "signing key and a SAC also makes API_KEY mandatory, on every network including testnet.",
    )


def report(dispute: DisputeRecord | None, dispute_id: str, amount: float | None, fallback: int) -> int:
    """What the STORE says happened to the money, and the exit code that follows.

    The record beats the call, always. A submission that timed out after the
    claim was taken leaves the dispute in `crediting` whatever the caller
    returned or raised, and that is the case an operator must not misread at
    2am — so `crediting` wins first, `credited` next, and only a state that says
    nothing about the money defers to what the call itself reported.

    `amount` is None on a rating-only run — a dispute credited by an earlier
    one — and the credit is then reported as that earlier run's, so a re-run
    for the rating can never be read as a second payment.

    EXIT_OK here is the REFUND half only. Since 4.04 a `credited` dispute with
    its refund on record is where the run's verdict begins, not where it ends:
    `execute` hands it straight to `report_rating`, whose code is the run's.
    """
    if dispute is None:
        say()
        say(f"  dispute {dispute_id} is not in the store after the uphold call.")
        say("  Check the payer's account on-chain before doing anything else.")
        say()
        return EXIT_UNEXPECTED if fallback == EXIT_OK else fallback

    if dispute.status == "crediting":
        return unresolved_credit(dispute, EXIT_TIMEOUT, "THE TRANSFER TIMED OUT — IT MAY STILL LAND.", amount)

    if dispute.status == "credited" and dispute.refund_tx:
        say()
        if amount is None:
            say(f"  CREDITED EARLIER — {dispute.payer} was paid by an earlier run; this one signed no transfer")
        else:
            say(f"  CREDITED — {amount:.7f} USDC paid to {dispute.payer}")
        say(f"  status:    {dispute.status}")
        say(f"  tx:        {dispute.refund_tx}")
        say(f"  evidence:  {expert_url('tx', dispute.refund_tx)}")
        say(f"  payer:     {expert_url('account', dispute.payer)}")
        say()
        say("  Funded by the platform's settler wallet. The disputed agent was NOT charged —")
        say("  label it that way wherever this hash is quoted.")
        say()
        return EXIT_OK

    if dispute.status == "upheld":
        say()
        # EXIT_TRANSFER_FAILED alongside EXIT_OK because they are the same
        # outcome reached two ways: `uphold` refusing with `refund_failed`, and
        # a call that returned while leaving the claim released. Both mean the
        # ledger rejected the transfer, so both get the sentence that matters —
        # nothing moved, and this is the one case where re-running is right.
        if fallback in (EXIT_OK, EXIT_TRANSFER_FAILED):
            say(f"  the transfer FAILED — dispute {dispute.id} is back at `upheld` and nothing moved.")
            say("  The claim was released, so running this again once the cause is fixed (settler")
            say("  balance, asset SAC, RPC) pays the credit. The log lines above name it.")
            say()
            return EXIT_TRANSFER_FAILED
        say(f"  dispute {dispute.id} stands at `upheld`: the decision was recorded, the credit was")
        say("  not paid, and no claim is held. Fix what the refusal above names, then re-run.")
        say()
        return fallback

    say()
    if fallback != EXIT_OK and dispute.status == "open":
        say(f"  dispute {dispute.id} is untouched at `open` — the refusal above landed before")
        say("  anything was written or signed.")
        say()
        return fallback
    say(f"  dispute {dispute.id} is {dispute.status!r} with refund tx {dispute.refund_tx or 'none'}.")
    say("  That is not a state this run can account for — reconcile the payer's account on-chain")
    say("  before re-running anything.")
    say()
    return EXIT_UNEXPECTED if fallback == EXIT_OK else fallback


async def read_standing(agent_id: str) -> reputation_svc.RepInfo | None:
    """The agent's reputation as the ledger holds it NOW, or None when it cannot be read.

    Read once before the uphold and once after, so the card's "the agent's
    dispute rate moves" is on the same screen as the transaction that moves
    it. A simulated read — no signature, no fee, nothing written — and never a
    reason for the run to fail: the transactions are the evidence, and this is
    a view of their effect. So it catches everything and answers None.

    The agent's cached score is dropped first. `fetch_rep` serves a read TTL,
    and the second read lands well inside the first one's window — without
    this it would print the pre-rating number beside a rating that landed.

    None rather than the prior when the read fails: `fetch_rep` answers an
    unreadable ledger with the cold-start prior marked `degraded`, and that
    prior printed as a dispute rate would give the agent a clean record the
    ledger may well contradict. With reads switched off it answers the prior
    too, undegraded, for the same non-reason, so that is None as well.
    """
    if not settings.reputation_enabled:
        return None
    try:
        reputation_svc.invalidate_rep(agent_id)
        info = await reputation_svc.fetch_rep(agent_id)
    except Exception as exc:
        logger.warning("could not read agent %s's reputation: %s: %s", agent_id, type(exc).__name__, exc)
        return None
    return None if info.degraded else info


def report_standing(agent_id: str, before: reputation_svc.RepInfo | None, after: reputation_svc.RepInfo | None) -> None:
    """The agent's dispute rate before this run and after it, and the movement.

    Printed whatever became of the rating: a rate that did not move beside a
    rating that did not land is the same story told by the ledger's counters,
    and one that moved beside a rating reported unconfirmed says it has most
    likely landed since. `disputed` and `count` are shown beside the rate
    because they are lifetime counters rather than decayed evidence — each
    landed dispute rating adds exactly one to both — so they are what an
    operator checks the movement against.
    """

    def _line(info: reputation_svc.RepInfo | None) -> str:
        if info is None:
            return "could not be read"
        return f"{info.dispute_rate_bps} bps   ({info.disputed} of {info.count} ratings disputed)"

    say(f"  dispute rate of {agent_id} on the ReputationLedger:")
    say(f"    before this run:  {_line(before)}")
    say(f"    after this run:   {_line(after)}")
    if before is not None and after is not None:
        moved = after.dispute_rate_bps - before.dispute_rate_bps
        say(f"    moved:            {moved:+d} bps" if moved else "    moved:            unchanged")
    else:
        if settings.reputation_enabled:
            say("    the ledger read failed — the WARNING above names why.")
        else:
            say("    reputation reads are switched off here (REPUTATION_ENABLED=false).")
        say("    A read never fails this run: the transactions are the evidence, and")
        say(f"    GET /api/stellar/reputation/{agent_id} re-reads the rate once the ledger is readable.")
    say()


def print_evidence(dispute: DisputeRecord) -> None:
    """Both of the dispute's on-chain artifacts, side by side, once both have landed.

    One upheld dispute leaves TWO transactions, and the deliverable is the pair:
    the credit that made the buyer whole and the rating that is the agent's
    consequence for it. Printed together, each labelled with who it moves, so
    the block pastes into the evidence bundle as one unit and neither hash is
    ever quoted without the other — the credit alone reads as the platform
    absorbing a failure, the rating alone as a score with nothing behind it.
    """
    say("  " + "=" * 74)
    say(f"  ON-CHAIN EVIDENCE — dispute {dispute.id}: both transactions, together")
    say(f"    refund: {expert_url('tx', dispute.refund_tx or '')}")
    say("            the platform credits the buyer — settler-funded; the agent is NOT charged")
    say(f"    rating: {expert_url('tx', dispute.rating_tx or '')}")
    say(f"            the agent's consequence — a dispute rating against {dispute.agent_id}")
    say("  " + "=" * 74)
    say()


@contextmanager
def watch_rating() -> Iterator[list[dispute_rating.RatingOutcome]]:
    """Collect every dispute-rating outcome `uphold` produces while this is open.

    `uphold` answers with the dispute record alone, deliberately: for an API
    caller the durable record is the one answer, and `credited` without a
    `rating_tx` is "paid, not fully resolved". What the record cannot say is
    which of several things an operator is looking at. A `rating_tx` is written
    for a rating that LANDED and equally for one that timed out IN FLIGHT, so
    the record alone can never confirm a rating. An empty one covers a FAILED
    rating, a COLLISION, a rating never submitted, and one that landed but
    could not be recorded. The service names which in an ERROR line; this
    script needs it as an exit code, and must not guess it from the record.

    So it watches the one call that knows. `submit_dispute_rating` returns the
    frozen `RatingOutcome` — the ledger's own answer to this run's attempt —
    and it is reached through the module attribute on every call, so wrapping
    that attribute sees exactly what the service saw and changes nothing: the
    outcome goes back untouched, and an exception goes through unrecorded.
    Restored on the way out, however the block exits. No outcome seen means
    nothing reached the ledger, and is reported as exactly that.

    The record still decides what is ON RECORD — `report_rating` reads the
    rating hash off the store, as `report` reads the refund's — and the outcome
    only says what the chain answered.
    """
    seen: list[dispute_rating.RatingOutcome] = []
    submit = dispute_rating.submit_dispute_rating

    async def _watched(*args: Any, **kwargs: Any) -> dispute_rating.RatingOutcome:
        outcome = await submit(*args, **kwargs)
        seen.append(outcome)
        return outcome

    dispute_rating.submit_dispute_rating = _watched
    try:
        yield seen
    finally:
        dispute_rating.submit_dispute_rating = submit


# What became of the dispute rating, as far as one run can know it:
#   rated        — landed now, and its hash is on the record;
#   confirmed    — an earlier attempt's, still on the record, which the ledger
#                  proved landed by refusing this run's copy as a replay;
#   unrecorded   — landed now, but the record write after it failed;
#   unconfirmed  — submitted and timed out: it may still land;
#   failed       — the ledger refused it, and nothing was written;
#   collision    — a replay with no attempt of this dispute's on record (D4);
#   unattempted  — no answer from the ledger at all: the rating was never
#                  submitted (not configured, or not formable from the
#                  records) or the submit raised, and the service's log lines
#                  say which.
RatingVerdict = Literal["rated", "confirmed", "unrecorded", "unconfirmed", "failed", "collision", "unattempted"]


def rating_verdict(dispute: DisputeRecord, outcome: dispute_rating.RatingOutcome | None) -> RatingVerdict:
    """Classify the rating from the record `uphold` left and the answer it drew.

    The record is read FIRST for everything it can settle. A SUCCESS is only
    `rated` once the store holds that very hash, because the record is what
    the API serves and what the next run judges a replay against. A REPLAY is
    split exactly as the service splits it (D4) — on whether this dispute
    already records an attempt — so the two can never disagree about whether a
    collision happened: the service answers both with the record unchanged, so
    the `rating_tx` read back here is the one it judged by.
    """
    if outcome is None:
        return "unattempted"
    if outcome.status == "SUCCESS" and outcome.tx_hash:
        return "rated" if dispute.rating_tx == outcome.tx_hash else "unrecorded"
    if outcome.status == "REPLAY":
        return "confirmed" if dispute.rating_tx else "collision"
    # A SUCCESS with no hash is the same unknown the rating service already
    # files as a TIMEOUT: nothing a reviewer could open says it landed.
    if outcome.status in ("SUCCESS", "TIMEOUT"):
        return "unconfirmed"
    return "failed"


def rating_not_landed(
    dispute: DisputeRecord, verdict: RatingVerdict, outcome: dispute_rating.RatingOutcome | None, rating_id: str
) -> int:
    """The block for a credit that landed and a rating that did not — and why re-running is RIGHT.

    Written against the 4.03 timeout block on purpose. An operator who has
    learned "never re-run after a timeout" from a transfer will apply it here,
    and here it is wrong: `uphold` never signs a second transfer for a
    `credited` dispute, so a re-run goes past the refund to the rating alone,
    and the ledger refuses a second rating under the same id, so a late
    landing is answered with Replay and confirmed rather than doubled. Said in
    as many words, because the rule it overrides was said in capitals.
    """
    say()
    say("  " + "#" * 74)
    say("  #  THE BUYER HAS BEEN PAID — BUT THE AGENT'S DISPUTE RATING DID NOT LAND.")
    say("  #  RE-RUNNING THIS SCRIPT IS SAFE HERE: IT RETRIES THE RATING, NEVER THE REFUND.")
    say("  " + "#" * 74)
    say()
    if verdict == "unconfirmed":
        say("  rating:    TIMED OUT — submitted and unconfirmed; it may still land.")
    elif verdict == "failed":
        say("  rating:    FAILED — the ledger refused it; nothing was written.")
    else:
        say("  rating:    NO ANSWER — nothing reached the ledger, or the submit never completed.")
        say("             The log lines above name why, and whether a re-run alone can mend it.")
    if outcome is not None and outcome.tx_hash:
        say(f"  its tx:    {outcome.tx_hash}")
        say(f"  check it:  {expert_url('tx', outcome.tx_hash)}")
    elif dispute.rating_tx:
        say(f"  on record: {dispute.rating_tx}   an earlier attempt, never confirmed")
        say(f"  check it:  {expert_url('tx', dispute.rating_tx)}")
    say(f"  agent:     {dispute.agent_id}")
    say(f"  dispute:   {dispute.id}")
    say(f"  rating id: {rating_id}")
    say()
    say(f"  Dispute {dispute.id} is `credited` with its refund on record, so the buyer is paid")
    say("  in full. It is NOT fully resolved: the agent's consequence is not on-chain yet.")
    say()
    say("  Why re-running is right here, when a timed-out CREDIT must never be re-run:")
    say("    * the refund cannot be paid twice — uphold never signs a second transfer for a")
    say("      `credited` dispute, so a re-run goes straight to the rating and does nothing else;")
    say("    * the rating cannot land twice — the ledger refuses a second one under the same")
    say("      rating id, so if this one lands late the re-run is told so (Replay) and confirms it.")
    say()
    say("  Re-run once the cause in the log lines above is fixed (after a timeout, once the")
    say("  network has caught up):")
    say(f"    python scripts/uphold_dispute.py --dispute-id {dispute.id}")
    say()
    return EXIT_RATING_NOT_LANDED


def rating_collision(dispute: DisputeRecord, rating_id: str) -> int:
    """The block for a rating the ledger refused as a replay of one this dispute never recorded (D4).

    Diagnosed from the LEDGER'S ANSWER, not from the record: `watch_rating`
    saw this run's submit come back as a Replay, and the record shows no
    attempt of this dispute's — the two facts the service itself judges a
    collision by. The record alone could never say it; an empty `rating_tx`
    looks the same after a failure.

    Loud, and never worded as a resolution: the credit stands, but the dispute
    records no rating of its own, and a dispute reported as resolved without
    its consequence is exactly what D4 forbids. It names the agent, the
    dispute and the rating id because those are what a lookup on the ledger
    starts from, and it links the ledger itself because that is where the
    lookup happens.

    Two causes, and the block gives both their remedies. The benign one is
    this dispute's own attempt that landed without its hash ever reaching the
    record — a timeout that returned none, or a store write that failed — and
    recording that hash resolves it. Anything else is a genuine collision, and
    a re-run cannot fix one: the ledger refuses every retry under this id.
    """
    say()
    say("  " + "#" * 74)
    say("  #  RATING COLLISION — THE AGENT'S REPUTATION CONSEQUENCE DID NOT LAND.")
    say(f"  #  agent {dispute.agent_id} · dispute {dispute.id}")
    say("  " + "#" * 74)
    say()
    say("  The ledger answered this run's rating with Replay: it already holds a rating for this")
    say("  agent under this rating id — and this dispute has no rating of its own on record.")
    say()
    say(f"  agent:     {dispute.agent_id}")
    say(f"  dispute:   {dispute.id}")
    say(f"  rating id: {rating_id}")
    say(f"  ledger:    {expert_url('contract', settings.stellar_reputation_ledger)}")
    say()
    say("  The credit stands — the buyer HAS been paid. The dispute is NOT resolved and must not")
    say("  be reported as resolved: it records no rating of its own, so none can be shown for it.")
    say()
    say("  Find the rating filed under that id on the ledger before anything else:")
    say("    * it is this dispute's own attempt, never recorded (a timeout that returned no hash,")
    say("      or a record write that failed) — record its hash, and the dispute is resolved:")
    say(f"      append_status({dispute.id!r}, 'credited', rating_tx=<hash>)")
    say("    * it is anything else — a genuine collision. Escalate it. Re-running cannot fix it:")
    say("      the ledger refuses every retry under this id.")
    say()
    return EXIT_RATING_COLLISION


def rating_unrecorded(dispute: DisputeRecord, tx_hash: str, rating_id: str) -> int:
    """The block for a rating that LANDED while the write recording it failed.

    Both transactions are on-chain, so the evidence exists — but the dispute
    does not carry the rating's hash, and that record is what the API serves
    and what the next run judges a replay against. Left alone, a re-run would
    be refused as a Replay with no attempt on record and report this dispute's
    own rating as a COLLISION. So it exits on the catch-all, the way a
    `credited` dispute with no refund hash does, and prints the one write that
    closes it before anything is re-run.
    """
    say()
    say("  " + "#" * 74)
    say("  #  THE RATING LANDED — BUT THE DISPUTE DOES NOT RECORD IT.")
    say("  #  RECORD IT BEFORE ANY RE-RUN, OR THE RE-RUN WILL REPORT A COLLISION.")
    say("  " + "#" * 74)
    say()
    say(f"  rating tx: {tx_hash}")
    say(f"  evidence:  {expert_url('tx', tx_hash)}")
    say(f"  rating id: {rating_id}")
    say()
    say("  The ledger confirmed the rating; the store write that records it on the dispute")
    say("  failed, and the log lines above name why. Close it by recording what landed:")
    say(f"    append_status({dispute.id!r}, 'credited', rating_tx={tx_hash!r})")
    say()
    return EXIT_UNEXPECTED


def derived_id_hex(dispute: DisputeRecord) -> str:
    """The rating id, for a report line with no `RatingOutcome` to read it from.

    Never raises: it is reached after money has moved, and the preview has
    already refused a dispute whose id will not derive — so a failure here is
    a record that changed mid-run, and the report says so in place of the id
    rather than dying with the verdict half-printed.
    """
    try:
        return dispute_rating.dispute_job_id(bytes.fromhex(dispute.job_id_hex), dispute.step_index).hex()
    except ValueError:
        return f"(none derives from job {dispute.job_id_hex!r})"


def report_rating(dispute: DisputeRecord, outcome: dispute_rating.RatingOutcome | None) -> int:
    """What became of the dispute rating, and the exit code that follows (4.04).

    Reached only once `report` has found the credit landed AND recorded: the
    rating is written after `credited` and never before (D3), so a run whose
    refund did not land has no rating to speak of.

    EXIT_OK only when a rating hash is on the record and the ledger has vouched
    for it during this run — by confirming it, or by refusing a second copy.
    Everything short of that is one of the three blocks above, because a
    dispute whose rating is not on-chain must never read as resolved.
    """
    verdict = rating_verdict(dispute, outcome)
    rating_id = outcome.job_id_hex if outcome is not None else derived_id_hex(dispute)
    if verdict == "collision":
        return rating_collision(dispute, rating_id)
    if verdict == "unrecorded" and outcome is not None and outcome.tx_hash:
        return rating_unrecorded(dispute, outcome.tx_hash, rating_id)
    if verdict not in ("rated", "confirmed") or outcome is None or not dispute.rating_tx:
        return rating_not_landed(dispute, verdict, outcome, rating_id)

    say()
    if verdict == "rated":
        say(f'  RATED — {dispute.agent_id} rated {outcome.rating}/100, kind "dispute", by this run')
    else:
        say(f'  RATED — {dispute.agent_id} rated {outcome.rating}/100, kind "dispute", by an earlier run;')
        say("           the ledger refused this run's copy as a replay, which is what confirms it landed.")
    say(
        f"  weight:    {outcome.weight_stroops} stroops"
        f" = {outcome.weight_stroops / reputation_svc.STROOPS_PER_USDC:.7f} USDC"
    )
    say(f"  rating tx: {dispute.rating_tx}")
    say(f"  evidence:  {expert_url('tx', dispute.rating_tx)}")
    say(f"  rating id: {rating_id}   filed under the job's own first 8 bytes")
    say(f"  job id:    {dispute.job_id_hex}")
    say()
    return EXIT_OK


async def execute(dispute_id: str, agent_id: str, amount: float | None) -> int:
    """Uphold the dispute, pay the credit, rate the agent, and report both from the store.

    Every path — clean return, refusal, unexpected exception — falls through to
    the same read, because the dispute's own record is the only thing that knows
    whether money moved. A caller that trusted the return value would call a
    timed-out transfer a failure and re-run it.

    The rating is reported only when the refund's report comes back clean —
    credited, with its hash on record — because that is the only state in
    which `uphold` rates at all (D3). Any other refund verdict is the whole
    story of the run, and its exit code stands.
    """
    store = get_dispute_store()
    fallback = EXIT_OK
    # Before the uphold, so the "before" is the ledger as it stood when nothing
    # of this run's had been written.
    before = await read_standing(agent_id)
    with watch_rating() as ratings:
        try:
            await dispute_svc.uphold(dispute_id)
        except dispute_svc.DisputeError as exc:
            # Every refusal on this path arrives as a `DisputeError`, including
            # the two the refund service raises: `uphold` catches
            # `RefundRefused`, releases the claim and re-raises it in this
            # vocabulary. So there is one except clause here and not two, and
            # the code carries through. A rating never arrives here: once the
            # buyer is paid, `uphold` answers with the record, never a raise.
            fallback = _REFUSAL_EXITS.get(exc.code, EXIT_NOT_ADJUDICABLE)
            if exc.code in _POST_SIGNING_CODES:
                say()
                say(f"  {exc.code}: {exc.message}")
            else:
                fallback = refuse(fallback, exc.code, exc.message)
        except Exception as exc:
            # Deliberately broad on a money path: an exception nobody
            # anticipated says nothing about whether the transfer was
            # submitted, and letting it reach the terminal as a traceback
            # invites exactly the retry that D3 forbids. It is reported, then
            # the store is asked.
            fallback = EXIT_UNEXPECTED
            say()
            say(f"  the uphold call raised {type(exc).__name__}: {exc}")
            say("  Do NOT re-run yet. The dispute's state below is the only thing that knows")
            say("  whether anything was signed.")

    dispute = await store.get_dispute(dispute_id)
    code = report(dispute, dispute_id, amount, fallback)
    if code != EXIT_OK or dispute is None:
        return code
    code = report_rating(dispute, ratings[-1] if ratings else None)
    report_standing(agent_id, before, await read_standing(agent_id))
    if code == EXIT_OK:
        print_evidence(dispute)
    return code


def build_parser() -> argparse.ArgumentParser:
    """The CLI, arguments and all.

    The description is where an operator who ran `--help` instead of reading the
    module finds the two facts that change what they are about to do: that this
    spends real funds, and that the platform — not the disputed agent — is the
    one spending them. The epilog is where they find what to do AFTER it, and
    it lists the post-signature codes because those are the ones where the
    right next move differs — two of them in opposite directions.

    Whole, rather than a parser `main` then adds arguments to, so that what a
    test reads out of `format_help()` is the text an operator sees.
    """
    parser = argparse.ArgumentParser(
        prog="uphold_dispute.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Uphold one dispute, pay its settler-funded credit, then write the agent's dispute\n"
            "rating, and print the evidence for both (stories 4.03 and 4.04).\n"
            "\n"
            "THIS MOVES REAL FUNDS on the network this process is configured for.\n"
            "\n"
            "The credit is FUNDED BY THE PLATFORM, not clawed back from the agent: the escrow\n"
            "never takes custody, so an upheld dispute is a new transfer out of the settler's\n"
            "own wallet to the buyer. The disputed agent's only consequence is reputational:\n"
            "a dispute rating on the ReputationLedger, written once the credit has landed.\n"
        ),
        epilog=(
            "exit codes after a live run's signature:\n"
            "   0  credit and rating both landed — the two links printed are the evidence\n"
            "   9  the credit FAILED, nothing moved — re-run once the cause is fixed\n"
            "  10  the credit TIMED OUT and may still land — NEVER re-run; reconcile it\n"
            "  12  the buyer IS paid but the rating did not land — re-running is SAFE and\n"
            "      retries the rating only, never the refund\n"
            "  13  rating collision — the agent's consequence did not land; look it up\n"
            "every other non-zero code is a refusal before anything was signed, except 11,\n"
            "which asks for the state it prints to be reconciled by hand.\n"
        ),
    )
    parser.add_argument(
        "--dispute-id",
        required=True,
        help="the dispute to uphold, as GET /api/disputes/{id} reports it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "resolve everything and print exactly what WOULD be paid and rated — the dispute, the "
            "settled step, D4's three bounds and which one binds, D5's cap and the payer, then the "
            "rating's value, weight and derived id — and stop. Signs nothing, submits nothing, reads "
            "nothing from the chain, and needs no signing key. Always run this first."
        ),
    )
    return parser


async def run(dispute_id: str, dry_run: bool) -> int:
    """Resolve, preview, and — unless this is a dry run — pay and rate. One event loop.

    One loop for the whole run rather than an `asyncio.run` per step, because
    the Postgres store keeps a connection pool bound to the loop that created
    it: a second `asyncio.run` would hand the paying path a pool whose loop had
    already closed, and it would fail there rather than here.
    """
    dispute, settlement = await resolve(dispute_id)
    if dispute is None:
        return refuse(
            EXIT_UNKNOWN_DISPUTE,
            "unknown_dispute",
            f"no dispute {dispute_id!r} in this store.",
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
    status_code = check_status(dispute)
    if status_code != EXIT_OK:
        return status_code

    # A credited dispute has nothing left to pay, so there is no credit to plan:
    # its run is the rating alone, and the preview says so rather than showing
    # a payout that will not happen.
    rating_only = dispute.status == "credited"
    amount: float | None = None
    if not rating_only:
        amount, code = plan(dispute, settlement, step)
        if amount is None:
            return code
    if not preview_rating(dispute, step):
        return refuse(
            EXIT_UNEXPECTED,
            "rating_not_derivable",
            f"dispute {dispute.id} can never be rated, so it could never be fully resolved.",
            "Nothing is signed until that is understood — the line above names what is missing.",
        )

    if dry_run:
        say()
        say("  DRY RUN — nothing was signed and nothing moved.")
        if rating_only:
            say("  Re-run WITHOUT --dry-run to write, or re-confirm, exactly the rating above. No transfer.")
        else:
            say("  Re-run WITHOUT --dry-run to pay exactly the credit above, then write exactly the rating above.")
        say()
        return EXIT_OK

    config_code = check_config()
    if config_code != EXIT_OK:
        return config_code
    return await execute(dispute_id, dispute.agent_id, amount)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args.dispute_id, args.dry_run))


if __name__ == "__main__":
    install_logging()
    sys.exit(main())
