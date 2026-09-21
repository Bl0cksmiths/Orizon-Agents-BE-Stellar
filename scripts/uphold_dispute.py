#!/usr/bin/env python3
"""Uphold one dispute, pay its credit, and print the on-chain evidence (story 4.03).

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
import logging
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

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
    from app.services import dispute_rating, dispute_svc, refund_svc, reputation_svc  # noqa: E402
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
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("  [%(levelname)s] %(name)s: %(message)s"))
    handler.addFilter(SecretRedactionLogFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


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
    """EXIT_OK while this dispute may still be paid; a refusal code otherwise.

    `claim_refund` is the real lock and refuses every one of these on its own
    (D2). This gate exists so the operator gets a sentence rather than a silent
    no-op — and so a preview says the same thing a live run would, which is the
    only reason to trust a preview at all.
    """
    if dispute.status == "credited":
        lines = [f"dispute {dispute.id} has already been credited — paying it again pays the buyer twice."]
        if dispute.refund_tx:
            lines += [f"refund tx:  {dispute.refund_tx}", f"evidence:   {expert_url('tx', dispute.refund_tx)}"]
        else:
            lines.append("No refund tx is recorded against it, which is worth reconciling before anything else.")
        return refuse(EXIT_NOT_ADJUDICABLE, "already_credited", *lines)
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
    """EXIT_OK when this process could actually sign a credit; a refusal otherwise.

    Checked before the uphold call rather than left to the service, so an
    operator who forgot one environment variable learns it from a sentence
    instead of from a stack trace out of the signing path — and learns it
    BEFORE a dispute has been moved to `upheld` by a run that then cannot pay.

    Presence only. No value is printed and the signing key is not even read:
    that a secret is set is the whole of what this needs to know.

    The ledger is on the list because the rating is half of what a live run is
    for (4.04): without it the credit would land and its rating could not, and
    the run would end with the buyer paid and the dispute unresolvable until
    somebody noticed. Refused here instead, while nothing has been signed.
    """
    missing = [
        name
        for name, present in (
            ("DISPUTE_REFUNDS_ENABLED=true", settings.dispute_refunds_enabled),
            ("STELLAR_SIGNING_KEY (the funded settler)", bool(settings.stellar_signing_key.strip())),
            ("STELLAR_ASSET_SAC", bool(settings.stellar_asset_sac.strip())),
            ("STELLAR_REPUTATION_LEDGER (the rating's ledger)", bool(settings.stellar_reputation_ledger.strip())),
        )
        if not present
    ]
    if not missing:
        return EXIT_OK
    return refuse(
        EXIT_NOT_CONFIGURED,
        "not_configured",
        "this process cannot sign a credit. Missing:",
        *(f"    - {name}" for name in missing),
        "Set them, re-run with --dry-run, and only then live. Turning the switch on beside a",
        "signing key and a SAC also makes API_KEY mandatory, on every network including testnet.",
    )


def report(dispute: DisputeRecord | None, dispute_id: str, amount: float, fallback: int) -> int:
    """What the STORE says happened to the money, and the exit code that follows.

    The record beats the call, always. A submission that timed out after the
    claim was taken leaves the dispute in `crediting` whatever the caller
    returned or raised, and that is the case an operator must not misread at
    2am — so `crediting` wins first, `credited` next, and only a state that says
    nothing about the money defers to what the call itself reported.
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


@contextmanager
def watch_rating() -> Iterator[list[dispute_rating.RatingOutcome]]:
    """Collect every dispute-rating outcome `uphold` produces while this is open.

    `uphold` answers with the dispute record alone, deliberately: for an API
    caller the durable record is the one answer, and `credited` without a
    `rating_tx` is "paid, not fully resolved". What the record cannot say is
    which of three different things an operator is looking at. A `rating_tx`
    is written for a rating that LANDED and equally for one that timed out IN
    FLIGHT; an empty one means a FAILED rating or a COLLISION. The service
    names which in an ERROR line, and this script needs it as an exit code.

    So it watches the one call that knows. `submit_dispute_rating` returns the
    frozen `RatingOutcome`, and it is reached through the module attribute on
    every call, so wrapping that attribute sees exactly what the service saw
    and changes nothing: the outcome goes back untouched, and an exception goes
    through unrecorded. Restored on the way out, however the block exits.

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
#   unattempted  — no answer from the ledger at all: the submit raised, or was
#                  never reached, and the service's log lines say which.
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
    if outcome.status == "SUCCESS":
        return "rated" if outcome.tx_hash and dispute.rating_tx == outcome.tx_hash else "unrecorded"
    if outcome.status == "REPLAY":
        return "confirmed" if dispute.rating_tx else "collision"
    if outcome.status == "TIMEOUT":
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
        say("  rating:    NO ANSWER — the submit never completed; the log lines above name why.")
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


async def execute(dispute_id: str, amount: float) -> int:
    """Uphold the dispute, pay the credit, and report the verdict from the store.

    Every path — clean return, refusal, unexpected exception — falls through to
    the same read, because the dispute's own record is the only thing that knows
    whether money moved. A caller that trusted the return value would call a
    timed-out transfer a failure and re-run it.
    """
    store = get_dispute_store()
    fallback = EXIT_OK
    try:
        await dispute_svc.uphold(dispute_id)
    except dispute_svc.DisputeError as exc:
        # Every refusal on this path arrives as a `DisputeError`, including the
        # two the refund service raises: `uphold` catches `RefundRefused`,
        # releases the claim and re-raises it in this vocabulary. So there is
        # one except clause here and not two, and the code carries through.
        fallback = _REFUSAL_EXITS.get(exc.code, EXIT_NOT_ADJUDICABLE)
        if exc.code in _POST_SIGNING_CODES:
            say()
            say(f"  {exc.code}: {exc.message}")
        else:
            fallback = refuse(fallback, exc.code, exc.message)
    except Exception as exc:
        # Deliberately broad on a money path: an exception nobody anticipated
        # says nothing about whether the transfer was submitted, and letting it
        # reach the terminal as a traceback invites exactly the retry that D3
        # forbids. It is reported, then the store is asked.
        fallback = EXIT_UNEXPECTED
        say()
        say(f"  the uphold call raised {type(exc).__name__}: {exc}")
        say("  Do NOT re-run yet. The dispute's state below is the only thing that knows")
        say("  whether anything was signed.")

    return report(await store.get_dispute(dispute_id), dispute_id, amount, fallback)


def build_parser() -> argparse.ArgumentParser:
    """The CLI, arguments and all.

    The description is where an operator who ran `--help` instead of reading the
    module finds the two facts that change what they are about to do: that this
    spends real funds, and that the platform — not the disputed agent — is the
    one spending them.

    Whole, rather than a parser `main` then adds arguments to, so that what a
    test reads out of `format_help()` is the text an operator sees.
    """
    parser = argparse.ArgumentParser(
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
    parser.add_argument(
        "--dispute-id",
        required=True,
        help="the dispute to uphold, as GET /api/disputes/{id} reports it",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "resolve everything and print exactly what WOULD be paid — the dispute, the settled "
            "step, D4's three bounds and which one binds, D5's cap and the payer — then stop. "
            "Signs nothing, submits nothing, and needs no signing key. Always run this first."
        ),
    )
    return parser


async def run(dispute_id: str, dry_run: bool) -> int:
    """Resolve, preview, and — unless this is a dry run — pay. One event loop.

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

    amount, code = plan(dispute, settlement, step)
    if amount is None:
        return code
    if not preview_rating(dispute, step):
        return refuse(
            EXIT_UNEXPECTED,
            "rating_not_derivable",
            f"dispute {dispute.id} could be credited but never rated, so it could never be fully resolved.",
            "Nothing is paid until that is understood — the line above names what is missing.",
        )

    if dry_run:
        say()
        say("  DRY RUN — nothing was signed and nothing moved.")
        say("  Re-run WITHOUT --dry-run to pay exactly the credit above, then write exactly the rating above.")
        say()
        return EXIT_OK

    config_code = check_config()
    if config_code != EXIT_OK:
        return config_code
    return await execute(dispute_id, amount)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args.dispute_id, args.dry_run))


if __name__ == "__main__":
    install_logging()
    sys.exit(main())
