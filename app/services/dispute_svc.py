"""When a buyer may dispute a settled step, what it records, and how it is paid.

Stories 4.02 to 4.04, ADR 0002. This module is the gate in front of the money:
every credit the platform pays starts as a `DisputeRecord` written here and
leaves through `uphold` here, so every rule that decides whether a dispute may
exist and whether it may be paid lives in this one file, stated once, in an
order whose reasoning is written down beside it (see `open_dispute` for the
first and `uphold` for the second — in both, THE ORDER OF THE CHECKS IS THE
DELIVERABLE).

Three modules, three jobs, and keeping them apart is what makes each reviewable:

  - `external_binding` holds the PROOF — the challenge, the nonce lifecycle and
    the signature check, shared with bind and unbind;
  - `dispute_store` holds the RECORDS — the settlement a dispute is judged
    against and the dispute itself, durably, because a window measured in hours
    outlives the process that promised it;
  - this module holds the RULES, and owns the vocabulary the API answers with.

OPENING a dispute touches no chain, and that is a deliberate boundary rather
than an accident of scope: a dispute is a CLAIM. `open_dispute` must therefore
cost no RPC, submit no transaction and touch no reputation —
`tests/test_dispute_svc.py` asserts that rather than leaving it as a claim.

ADJUDICATING one is where that changes, and only there. `uphold` signs a
settler-funded transfer through `refund_svc`, under an adjudication this sprint
performs off-chain (ADR 0002's disclosed trust model), and once that credit has
landed it writes the dispute rating through `dispute_rating` (4.04). The credit
is FUNDED BY THE PLATFORM and is never clawed back from the agent — the
deployed escrow takes no custody, so there is nothing to reverse — and every
artifact a buyer can see has to say so (SOW §3.8). The rating is the agent's
consequence instead: a low score on the ReputationLedger that costs it future
routing.

The authority model in one line: THE PAYER PROVES THEMSELVES WITH A WALLET
SIGNATURE, checked against the payer recorded on the settlement at the moment it
settled. Not the in-memory task token — that dies with the process, and a
24 h window that a restart voids is not a window. The signature mirrors endpoint
binding, so a buyer who has already connected a wallet to pay has nothing new to
learn, and the service holds no credential it could lose.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from ..agents.workers.prompt_safety import sanitize_untrusted
from ..config import settings
from ..schemas import TraceLevel, TraceLine
from ..state import state
from ..trace_bus import bus
from . import dispute_rating, rating_writer, refund_svc, reputation_svc
from . import external_binding as eb
from .dispute_store import (
    DisputeRecord,
    DuplicateDisputeError,
    SettlementRecord,
    get_dispute_store,
    new_dispute_id,
)

logger = logging.getLogger(__name__)

# Told the ledger's own answer to a dispute rating, for an in-process caller
# that needs more than the record can say. The record answers "is a rating on
# file" and, since 4.06, "is it known to have landed" (`rating_confirmed`) —
# but not what THIS attempt drew: a FAILED rating and a collision both leave
# the record exactly as it was, and a timeout that replaced an earlier dead
# hash reads just like the one before it, so a tool reporting evidence to a
# human cannot tell them apart from the record alone. `uphold` keeps its
# single return type on purpose — an API response must never disagree with a
# later GET of the same dispute — so the answer is handed out through this
# declared seam instead, and only to a caller that asks for it.
RatingObserver = Callable[[dispute_rating.RatingOutcome], None]

# Re-exported so the router depends on ONE module for the whole dispute flow and
# the message the frontend shows is the message the verifier checks. Aliases
# rather than wrappers: a second copy of the format is a second thing to keep in
# step, and the format only stays trustworthy while there is exactly one.
DISPUTE_MESSAGE_PREFIX = eb.DISPUTE_MESSAGE_PREFIX
dispute_message = eb.dispute_message

# `secrets.token_hex(16)`, so 32 hex characters. Checked before the nonce is
# looked up: the value is caller-supplied, and a table lookup is not the place
# to discover that somebody sent a megabyte.
NONCE_HEX_CHARS = 32

# An ed25519 signature is 64 bytes — 88 characters in base64. Bounded before the
# decode for the reason `BindReq.signature` gives: 256 characters is ~3x what a
# real signature needs, so nothing legitimate is refused and nothing enormous is
# decoded.
_SIGNATURE_BYTES = 64
_MAX_SIGNATURE_CHARS = 256

# A dispute reason is MANDATORY (4.02 AC) and bounded. 500 characters mirrors
# `DecomposeRequest.intent`, which is this repo's existing answer to "how much
# free text is a field allowed to be": enough to say what was wrong with a
# step's output, not enough to write a novel into a store whose in-memory
# fallback holds 500 records and whose rows are read back into every dispute
# listing.
MAX_REASON_CHARS = 500


class DisputeError(Exception):
    """A dispute was refused, and why — in the vocabulary the API answers with.

    `code` is a stable snake token the frontend maps to a message and the
    envelope in `app/main.py` reproduces verbatim; `status_code` is the HTTP
    status that code always carries, kept here rather than in the router so the
    two cannot drift and so every caller of this service agrees on what a given
    refusal means. `message` is for a human and may name specifics (the time a
    window closed); it is never the place to put anything the caller has not
    already proved they may know.

    `existing` carries the ORIGINAL dispute on a duplicate. That is not an error
    the buyer can act on — they already disputed this step — so the router
    answers it with the dispute they already have, unchanged.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        status_code: int = 400,
        existing: DisputeRecord | None = None,
    ) -> None:
        self.code = code
        # The envelope's own fallback (`main.http_exception_handler`) turns a
        # snake token into a human string exactly this way, so a refusal raised
        # without a message still reads as one rather than as an empty body.
        self.message = message or code.replace("_", " ")
        self.status_code = status_code
        self.existing = existing
        super().__init__(self.message)


def _refuse(code: str, status_code: int, message: str, job_id_hex: str, step_index: int) -> DisputeError:
    """Build a refusal and log it once, server-side, with the job it concerns.

    Returned rather than raised so the call site still reads `raise` — the
    refusals are the substance of this module and hiding them inside a helper
    would make them easy to miss. The reason token is logged but never the
    nonce or the signature: both are live credentials for their window.
    """
    logger.warning("dispute refused: job=%s step=%s reason=%s", job_id_hex, step_index, code)
    return DisputeError(code, message, status_code)


async def issue_dispute_challenge(job_id_hex: str, step_index: int) -> tuple[str, float]:
    """Mint the challenge a buyer signs to dispute `step_index` of `job_id_hex`.

    Returns (nonce, expires_at). Async because it reads the settlement store
    first, and it reads the settlement store for `bind_challenge`'s reason: the
    challenge table is bounded and its key is caller-supplied, so it may only
    ever hold pairs that could really be disputed. Without that, one public
    route would let anyone invent job ids and steps until the table evicts the
    challenges honest buyers and operators are mid-way through signing.

    It refuses an unknown job (`unknown_job`) and a step the settlement does not
    have (`step_not_settled`) — and NOTHING ELSE. Whether that step was
    delivered, whether the window is still open and whether a dispute already
    exists are facts about the workflow's private state, and they are answered
    in `open_dispute`, behind the signature. Minting a challenge for a dispute
    that will be refused costs the caller a round trip and tells them nothing.
    """
    settlement = await get_dispute_store().get_settlement(job_id_hex)
    if settlement is None:
        raise _refuse("unknown_job", 404, "no settled workflow with that job id", job_id_hex, step_index)
    if settlement.step(step_index) is None:
        raise _refuse("step_not_settled", 409, f"that workflow has no step {step_index}", job_id_hex, step_index)
    return eb.issue_dispute_challenge(job_id_hex, step_index)


def _authenticate_payer(
    settlement: SettlementRecord,
    step_index: int,
    payer: str,
    nonce: str,
    signature_b64: str,
) -> None:
    """Prove that the caller is the party whose money moved, or refuse.

    The authority is `settlement.payer` — written when the workflow settled,
    before any dispute existed — never the `payer` in the request, which is
    checked against it and otherwise unused. A dispute is the buyer's remedy
    and only the buyer's: nobody else may spend the platform's credit budget,
    and nobody else may put a dispute rating on an agent's record.

    Four steps, cheapest first, and each one refuses with its own code because
    the three failures need different things from the caller:

      1. the signature's SHAPE — a pure decode, no table touched, so a client
         bug costs nothing (`signature_malformed`, 400);
      2. the challenge is LIVE and is the one this caller was given
         (`challenge_expired`, 400). Distinguished from a bad signature on
         purpose: a buyer who spent a minute in a wallet dialog needs to be told
         to ask for a new challenge, and "not the payer" would send them looking
         for a problem with their wallet instead;
      3. the address the caller claims IS the recorded payer — a string compare
         before any crypto, and before anything can consume the nonce
         (`not_the_payer`, 403);
      4. the signature verifies against that recorded payer, which consumes the
         nonce (`not_the_payer`, 403).

    3 and 4 share one code deliberately. "That is not the payer's address" and
    "that is not the payer's signature" are the same fact to anyone entitled to
    an answer, and two codes would turn this into an oracle for which addresses
    paid for which jobs — job ids are public in the escrow's `charged` event,
    so an enumerator would need nothing but patience.
    """
    job_id_hex = settlement.job_id_hex
    if len(signature_b64) > _MAX_SIGNATURE_CHARS:
        raise _refuse("signature_malformed", 400, "that is not an ed25519 signature", job_id_hex, step_index)
    try:
        raw = base64.b64decode(signature_b64, validate=True)
    except ValueError:
        # binascii.Error is a ValueError, so one handler covers a non-base64
        # body without masking a real bug.
        raise _refuse("signature_malformed", 400, "the signature is not valid base64", job_id_hex, step_index) from None
    if len(raw) != _SIGNATURE_BYTES:
        raise _refuse(
            "signature_malformed",
            400,
            f"an ed25519 signature is {_SIGNATURE_BYTES} bytes",
            job_id_hex,
            step_index,
        )
    if len(nonce) != NONCE_HEX_CHARS or not eb.dispute_challenge_is_live(job_id_hex, step_index, nonce):
        raise _refuse(
            "challenge_expired",
            400,
            "that challenge has expired or was already used — ask for a new one",
            job_id_hex,
            step_index,
        )
    if payer != settlement.payer:
        raise _refuse("not_the_payer", 403, "only the payer of a workflow may dispute it", job_id_hex, step_index)
    if not eb.verify_dispute_challenge(job_id_hex, step_index, settlement.payer, signature_b64):
        raise _refuse("not_the_payer", 403, "only the payer of a workflow may dispute it", job_id_hex, step_index)


def _require_reason(reason: str, job_id_hex: str, step_index: int) -> str:
    """The buyer's reason, cleaned and bounded — or a refusal if there is none.

    `sanitize_untrusted` is this repo's existing primitive for text somebody
    else wrote (`app/agents/workers/prompt_safety.py`), used here for
    `registry_sync`'s reason rather than its own: it strips C0/C1 control
    characters — all but tab and newline, so a buyer may still write a
    paragraph — and clamps the length. The control characters are the part that
    matters for a dispute: this string is read back into an API response, shown
    in the console and quoted in the dispute receipt, and an escape sequence or
    a NUL in any of those forges structure that nobody wrote.

    Deliberately NOT `fence_untrusted`: a reason is a FIELD, not a prompt
    block, and nothing here sends it to a model. The prompt-fence side effects
    it does carry (collapsing `====` runs, redacting a forged BEGIN/END marker)
    cost a buyer nothing to live with and keep one primitive rather than two.

    It is also NOT an escaping function, and must not be mistaken for one: the
    console escapes on render, as it does for every other stored string. What
    this decides is what we STORE — evidence the buyer wrote, kept as close to
    verbatim as is safe.

    Empty after cleaning is a refusal, because "the buyer said what was wrong"
    is the whole evidentiary content of a dispute that a human will later
    adjudicate. Its code sits deliberately outside the frozen set of job-state
    codes: those describe the WORKFLOW's state and each one is final, while this
    one describes the request and the caller can fix it — 422, the status the
    router's own field bound produces for the same mistake.
    """
    cleaned = sanitize_untrusted(reason, max_chars=MAX_REASON_CHARS)
    if not cleaned:
        raise _refuse(
            "reason_required",
            422,
            "a dispute must say what was wrong with the step",
            job_id_hex,
            step_index,
        )
    return cleaned


def _duplicate(existing: DisputeRecord, job_id_hex: str, step_index: int) -> DisputeError:
    """The `duplicate_dispute` refusal, carrying the ORIGINAL dispute.

    One builder for both places that raise it — the pre-check and the store's
    race backstop — so the two cannot answer the same situation differently.
    Logged at INFO, not WARNING: a buyer pressing a button twice is not a
    security event, and the dispute they already have is a useful answer.
    """
    logger.info(
        "duplicate dispute: job=%s step=%s answered with %s",
        job_id_hex,
        step_index,
        existing.id,
    )
    return DisputeError(
        "duplicate_dispute",
        f"step {step_index} of this workflow was already disputed",
        409,
        existing,
    )


async def open_dispute(
    *,
    job_id_hex: str,
    step_index: int,
    reason: str,
    payer: str,
    nonce: str,
    signature_b64: str,
) -> DisputeRecord:
    """Open a dispute against one settled step, or refuse with a `DisputeError`.

    THE ORDER OF THE CHECKS BELOW IS LOAD-BEARING, not house style — the bind
    route's docstring makes the same point about the same kind of gate. Two
    principles decide it: cheapest first, and nothing about a workflow's private
    state is answered before the caller has proved they are its buyer.

      1. **The reason** — pure local text handling: no store read, no crypto,
         nothing disclosed, so it is the cheapest check there is. It is also
         the ONLY refusal here the buyer can fix and retry, which is why it
         must come before step 3 rather than at the end: verifying the
         signature CONSUMES the challenge, so a buyer refused for an empty
         reason afterwards would need a fresh nonce and a second trip through
         their wallet to send the same dispute again.
      2. **The settlement** — one store read, and every rule after it needs the
         record anyway: the payer to check the signature against, the stamped
         window, the step's price. An unknown job is answered before anything
         else because there is nothing to judge (`unknown_job`, 404). It
         discloses only what the chain already does — a settled job id is public
         in the escrow's `charged` event, and this says no more than "we hold a
         settlement for it".
      3. **The payer** — see `_authenticate_payer`. Everything after this point
         is off-chain state that belongs to the buyer: whether a step was
         delivered, when their window closes, whether they already disputed. A
         caller who cannot prove they are the buyer learns none of it.
      4. **The window** — judged on the closing time STAMPED on the settlement
         record, never recomputed from `settings.dispute_window_seconds`. The
         buyer was told a deadline at settlement time; tuning the setting
         afterwards must not move it for work already done, in either direction
         (`dispute_window_closed`, 409, and the message says when it closed).
      5. **The step** — it must exist on the settlement and have been delivered.
         A step that failed was never part of what the buyer paid for, so there
         is nothing to credit (`step_not_settled`, 409).
      6. **Money actually moved** — the workflow charged something on-chain and
         this step had a price (`nothing_was_charged`, 409). A credit is a real
         transfer out of the platform wallet, so a dispute of a step nobody paid
         for is a withdrawal request, not a remedy.
      7. **One dispute per step** — a second attempt is answered with the first
         dispute, unchanged (`duplicate_dispute`, 409, carrying it). Last
         because it is the only rule whose answer is a whole record, and the
         store re-checks it under the race (see below).

    Returns the stored `DisputeRecord` (status `open`). Writes nothing on-chain
    and touches no reputation: 4.03 pays the credit, 4.04 writes the rating.
    """
    reason = _require_reason(reason, job_id_hex, step_index)

    store = get_dispute_store()

    settlement = await store.get_settlement(job_id_hex)
    if settlement is None:
        raise _refuse("unknown_job", 404, "no settled workflow with that job id", job_id_hex, step_index)

    _authenticate_payer(settlement, step_index, payer, nonce, signature_b64)

    if time.time() > settlement.window_closes_at:
        # `>` rather than `>=`: a dispute arriving on the exact stamped second
        # is inside the window the buyer was promised. The record's own value,
        # and never `settled_at + settings.dispute_window_seconds` — a promise
        # that a configuration change can retroactively shorten is not one.
        closed_at = datetime.fromtimestamp(settlement.window_closes_at, timezone.utc).isoformat(timespec="seconds")
        raise _refuse(
            "dispute_window_closed",
            409,
            f"the dispute window for this workflow closed at {closed_at}",
            job_id_hex,
            step_index,
        )

    step = settlement.step(step_index)
    if step is None:
        raise _refuse("step_not_settled", 409, f"that workflow has no step {step_index}", job_id_hex, step_index)
    if not step.delivered:
        raise _refuse(
            "step_not_settled",
            409,
            f"step {step_index} produced no output, so nothing was charged for it",
            job_id_hex,
            step_index,
        )

    if settlement.settled_usdc <= 0:
        # `settled_usdc` is what actually moved on-chain, not the plan's
        # estimate: `charge` floors its total to dust, and a workflow whose
        # transfer never landed leaves nothing to credit back. Judged on the
        # settlement rather than the step because this is a fact about the
        # payment, and the payment is one transfer for the whole workflow.
        raise _refuse(
            "nothing_was_charged",
            409,
            "this workflow settled without charging anything, so there is nothing to credit",
            job_id_hex,
            step_index,
        )
    if step.price_usdc <= 0:
        # Same rule at step granularity, and the same code: a free step is not
        # a cheap one to dispute, it is one with no charge to credit back.
        raise _refuse(
            "nothing_was_charged",
            409,
            f"step {step_index} was free, so there is nothing to credit",
            job_id_hex,
            step_index,
        )

    existing = await store.find_dispute(job_id_hex, step_index)
    if existing is not None:
        # The buyer gets their own dispute back rather than an error they
        # cannot act on: one dispute per (job, step) is a product rule (a step
        # is credited once), and the second press of a button is not a failure.
        raise _duplicate(existing, job_id_hex, step_index)

    # R12, named here so the collision cannot be rediscovered the hard way: the
    # settler has ALREADY auto-rated this job under `Rated(agent_id, job_id)`,
    # and `ReputationLedger.submit` checks that replay guard before it reads
    # `kind`. So the rating an upheld dispute earns (story 4.04, `uphold`) is
    # written under `dispute_rating.dispute_job_id(job_id, step_index)` — a
    # derived id unique to this disputed step — or the submission would come
    # back `Error::Replay` and the dispute would never land. Opening one writes
    # nothing on-chain at all.
    record = DisputeRecord(
        id=new_dispute_id(),
        job_id_hex=job_id_hex,
        task_id=settlement.task_id,
        step_index=step_index,
        agent_id=step.agent_id,
        # The RECORDED payer, not the one in the request — they are equal by
        # now, and the record should carry the one the settlement proved.
        payer=settlement.payer,
        # The cleaned text, which is what every reader of this record gets.
        reason=reason,
        status="open",
        # The step's own price as it settled, never the plan's estimate: the
        # credit is computed from this, and a credit larger than what was
        # charged would be the platform paying for work it never billed.
        charged_usdc=step.price_usdc,
        creditable_usdc=refund_svc.credited_amount_usdc(step.price_usdc, settings.dispute_credited_fraction),
        opened_at=time.time(),
    )

    try:
        stored = await store.open_dispute(record)
    except DuplicateDisputeError as e:
        # The store enforces one dispute per (job, step) as well, and it is the
        # only check that holds under a race: two requests that both passed the
        # pre-check above still meet here, and the loser must get the winner's
        # dispute back rather than a 500. Same code, same shape, same record —
        # a race is not a different outcome, only a different path to it.
        raise _duplicate(e.existing, job_id_hex, step_index) from None
    logger.info(
        "dispute opened: id=%s job=%s step=%s agent=%s creditable=%.7f",
        stored.id,
        job_id_hex,
        step_index,
        step.agent_id,
        stored.creditable_usdc,
    )
    return stored


async def get_dispute(dispute_id: str) -> DisputeRecord | None:
    """One dispute by id, or None. The id is unguessable (`new_dispute_id`), so
    holding one is the authorization to read it — the same trade the receipt
    routes make, and the reason this needs no account."""
    return await get_dispute_store().get_dispute(dispute_id)


async def list_for_task(task_id: str) -> tuple[DisputeRecord, ...]:
    """Every dispute opened against a task, in the order they were opened."""
    return await get_dispute_store().list_disputes_for_task(task_id)


async def settlement_for_task(task_id: str) -> SettlementRecord | None:
    """The settlement a task produced, or None if it never settled.

    What the console needs to show a dispute button at all: the job id to
    challenge against, the steps that were charged, and the stamped
    `window_closes_at` that says whether there is still time.
    """
    return await get_dispute_store().get_settlement_by_task(task_id)


# ── adjudication: the money path (story 4.03, ADR 0002) ─────────
#
# Everything below decides whether the platform SIGNS A TRANSFER, so the order
# of the steps in `uphold` is the deliverable and not an implementation detail.
# Two facts shape all of it. `store.claim_refund` is the lock, taken before
# anything is signed and never a read-then-write (D2). And the Stellar client
# does not raise on failure — it returns a status, one of whose values means
# "submitted, may still land" (D3), which is the only way this service can pay
# a buyer twice.


def _refuse_credit(
    dispute: DisputeRecord,
    code: str,
    status_code: int,
    message: str,
    *,
    amount_usdc: float | None = None,
    tx_hash: str | None = None,
    level: int = logging.WARNING,
) -> DisputeError:
    """Build an adjudication refusal and log it with what a reconciler needs.

    Separate from `_refuse` because the two refuse different things and so must
    log different facts: that one refuses to OPEN a dispute and is keyed by
    (job, step), while this one refuses to PAY one and names the dispute, the
    job, the buyer and the amount — the four values somebody holding the ledger
    and a block explorer needs in order to decide whether money moved. The
    transaction hash joins them whenever there is one, because on this path the
    hash IS the evidence. Returned rather than raised for `_refuse`'s reason:
    the refusals are the substance of this module, and the call site should
    still read `raise`.

    `level` is WARNING for a refusal an adjudicator caused and asked for, and
    ERROR for one that leaves money in a state a human has to resolve. Nothing
    secret is logged and nothing secret is reachable from here: the payer is a
    public address, and the settler's signing key never enters this module.
    """
    logger.log(
        level,
        "refund refused: dispute=%s job=%s payer=%s amount=%s tx=%s reason=%s",
        dispute.id,
        dispute.job_id_hex,
        dispute.payer,
        "-" if amount_usdc is None else f"{amount_usdc:.7f}",
        tx_hash or "-",
        code,
    )
    return DisputeError(code, message, status_code)


async def _load_for_adjudication(dispute_id: str) -> DisputeRecord:
    """The dispute an adjudicator named, or `unknown_dispute` (404).

    Shared by `uphold` and `reject` so the two cannot answer an id that does
    not exist differently — a 404 from one and a 409 from the other would make
    the two routes disagree about the same fact. 404 discloses nothing here:
    the id is unguessable (`new_dispute_id`), and this route is adjudicator-only
    (D1) rather than something a stranger can probe.
    """
    record = await get_dispute_store().get_dispute(dispute_id)
    if record is None:
        logger.warning("adjudication refused: dispute=%s reason=unknown_dispute", dispute_id)
        raise DisputeError("unknown_dispute", "no dispute with that id", 404)
    return record


async def reject(dispute_id: str, *, note: str) -> DisputeRecord:
    """Adjudicate a dispute AGAINST the claim, from `open` and from nowhere else,
    and tell the buyer why.

    A rejection is terminal and it is the one outcome that must never become
    payable again: `store.claim_refund` only ever claims an `upheld` dispute,
    `uphold` refuses a `rejected` one outright, and `append_status("rejected")`
    drops the refund claim row — three independent places, because "the
    platform does not pay this one" is the kind of decision that must not
    depend on a single check holding.

    Every other status is refused rather than absorbed, including `rejected`
    itself. The alternative — answering a second rejection with the existing
    record, the way a duplicate `open_dispute` is answered — would quietly
    accept a second adjudicator overruling the first, and would also accept a
    rejection of a dispute that is mid-payout or already paid, which is the one
    thing an adjudicator most needs to be told they cannot do.

    That check is a READ, so the write it guards is made CONDITIONAL on it
    (`expected_status="open"`) rather than trusting it across the await that
    separates them. What lands in that gap is not hypothetical: an uphold
    claiming the dispute moves it to `crediting` and signs a transfer, and an
    unconditional append would then write `rejected` over a payment that is on
    the network AND drop the refund claim row — which is at once the mutex
    stopping a second transfer and the whole of the reconciliation queue for
    the first. Losing the compare-and-set means precisely that happened, so
    the rejection is refused, naming the status the store really holds.

    Deliberately NOT gated on `DISPUTE_REFUNDS_ENABLED` the way `uphold` is.
    That switch guards the platform's WALLET, and a rejection signs nothing and
    pays nothing; gating it would mean a deployment with the refund path off
    could not close a dispute at all, leaving buyers with claims nobody is
    allowed to answer. The route still refuses both under `require_adjudicator`
    (D1) — the switch is a money control here and an authorisation control
    there, and only one of those is this module's to make.

    `note` is REQUIRED, and it is written FOR THE BUYER. It is the explanation
    their receipt shows beside the word "rejected" (story 4.06), so it is
    written in words the buyer can read — never internal adjudication
    shorthand, and never anything about another dispute. It is mandatory
    because a rejection with no explanation is worse than no dispute system:
    the buyer's side of the argument is durable from the moment they raise it
    (`reason`, frozen there), an upheld dispute leaves an amount and a
    transaction hash behind, and a refusal that said nothing would hand the
    buyer the outcome they are most likely to contest with nothing in it to
    contest. A buyer told "no" without a reason learns only that complaining
    here is pointless.

    It is checked FIRST, before the dispute is even read: pure text handling
    is the cheapest check there is, and it is the only refusal here an
    adjudicator can fix and send again. A note that is empty AFTER cleaning —
    missing, blank, or nothing but control characters — is refused as
    `rejection_reason_required` (422, the status the buyer's own missing
    `reason` carries) before anything is written, because storing it would
    print an empty explanation on the receipt.

    It is cleaned HERE and nowhere else, because the store keeps what it is
    given byte for byte on purpose: bounding this and stripping the control
    characters out of it is this module's job, exactly as it is for the
    buyer's `reason` and for the same reason — it is read back into an API
    response and rendered in front of a person, where an escape sequence or a
    NUL forges structure nobody wrote.

    Redefining it from the audit-only note story 4.03 introduced is safe
    because nothing has ever been rejected: rejecting requires the refund path
    — the adjudication route refuses both decisions while
    DISPUTE_REFUNDS_ENABLED is off — and that path has never been enabled in
    production. No note exists that was written for an auditor and would now
    be shown to a buyer.

    It is still DELIBERATELY NOT LOGGED. `open_dispute` sets that convention
    and this follows it: free text about one complaint belongs on the record,
    never in the operator's log viewer, where it is unbounded, useless for
    reconstructing an incident, and — for the buyer's `reason`, which arrives
    over a public route — written by somebody else. The line below says that
    an explanation was recorded and whom the decision concerns; the
    explanation itself is read from the record by whoever needs it.
    """
    # `sanitize_untrusted` reads a None as "", so a caller that ignores the
    # annotation is refused below with the right code rather than with a
    # TypeError the API would answer as a 500.
    cleaned = sanitize_untrusted(note, max_chars=MAX_REASON_CHARS)
    if not cleaned:
        logger.warning("adjudication refused: dispute=%s reason=rejection_reason_required", dispute_id)
        raise DisputeError(
            "rejection_reason_required",
            "a rejection must tell the buyer why their dispute was not upheld",
            422,
        )

    dispute = await _load_for_adjudication(dispute_id)
    if dispute.status != "open":
        raise _refuse_credit(
            dispute,
            "dispute_not_open",
            409,
            f"this dispute is {dispute.status}, and only an open dispute can be rejected",
            amount_usdc=dispute.creditable_usdc,
            tx_hash=dispute.refund_tx,
        )
    rejected = await get_dispute_store().append_status(dispute_id, "rejected", note=cleaned, expected_status="open")
    if rejected is None:
        # The dispute moved under the read above. Refused rather than retried:
        # a rejection is an adjudicator's decision about a dispute in a
        # particular state, and the state it was decided about is gone.
        current = await _load_for_adjudication(dispute_id)
        raise _refuse_credit(
            current,
            "dispute_not_open",
            409,
            f"this dispute is {current.status}, and only an open dispute can be rejected",
            amount_usdc=current.creditable_usdc,
            tx_hash=current.refund_tx,
        )
    # `noted=` is the presence of the explanation and never its text. It reads
    # `yes` on every rejection now that none can be recorded without one, and
    # stays in the line so it reads the same as every rejection logged before.
    logger.info(
        "dispute rejected: id=%s job=%s step=%s payer=%s noted=%s",
        rejected.id,
        rejected.job_id_hex,
        rejected.step_index,
        rejected.payer,
        "yes" if rejected.note else "no",
    )
    return rejected


async def _trace_on_workflow(dispute: DisputeRecord, level: TraceLevel, what: str, msg: str) -> None:
    """Best effort: show one line about a resolved dispute on the workflow it disputes.

    THE DURABLE RECORD OF A DISPUTE'S OUTCOME IS THE DISPUTE, NOT THIS LINE.
    `app/state.py` keeps the newest 200 tasks and drops each one's traces with
    it, while a dispute window is 24 hours wide — so by the time one is
    adjudicated the workflow it disputes has usually been evicted, and this is
    decoration on the ones a console still has on screen. Nothing reads it
    back, nothing reconciles against it, and every caller emits it after the
    store already holds what it reports, so it can never be the reason a paid
    dispute looks unpaid.

    Which is exactly why it is GUARDED on `state.tasks` rather than simply
    appended. `state.append_trace` is `traces.setdefault(task_id, []).append(line)`,
    and for an evicted task that does not merely write where nobody looks: it
    RECREATES a `traces` entry whose task is gone, and eviction only ever
    removes traces alongside a task still in `task_order`, so nothing will
    remove it again. A dispute resolved hours later would leak one list per
    line for the life of the process, invisibly. Present task only, and never
    `setdefault` on an absent one.

    `state` and `bus` directly, not `execution_svc._emit`: that helper is keyed
    on the run's `time.monotonic()` start, which died with the request that
    held it, so it could not be reused here even if the import were free. And
    it would not be free — the module holding the dispute RULES would come to
    depend on the module that RUNS workflows, in the one direction ADR 0002
    keeps clear, for the sake of four lines. `TraceLine` and the bus are the
    whole of what the two actually share, so those are the whole of what this
    imports.

    `what` names the line in the warning a failed trace leaves, and nowhere
    else.
    """
    task = state.tasks.get(dispute.task_id)
    if task is None:
        return
    # The same elapsed-since-the-run-started clock every other line on this
    # task carries, so a credit sorts where it happened rather than at 00.000.
    elapsed = max(time.time() - task.started_at, 0.0)
    seconds, millis = divmod(int(elapsed * 1000), 1000)
    line = TraceLine(t=f"{seconds:02d}.{millis:03d}", level=level, msg=msg)
    try:
        state.append_trace(dispute.task_id, line)
        await bus.publish(dispute.task_id, line)
    except Exception:
        # What this line reports is already on the dispute record by the time
        # it runs. Letting a cosmetic line raise out of `uphold` would answer a
        # successful payout with a 500 and invite the one retry this whole
        # path exists to make safe, so it is logged and swallowed instead.
        logger.warning("could not trace the %s for dispute %s on task %s", what, dispute.id, dispute.task_id)


async def _note_credit_on_workflow(dispute: DisputeRecord, amount_usdc: float, tx_hash: str | None) -> None:
    """Show a landed credit on the workflow it came out of (`_trace_on_workflow`).

    `cost` because a refund is money, and the wording is the SOW §3.8 standard
    rather than a turn of phrase: the platform FUNDS this credit out of its own
    wallet, and the disputed agent keeps what it was paid. This is the only
    message about a refund the buyer ever sees, so it is the one that has to
    say so.
    """
    await _trace_on_workflow(
        dispute,
        "cost",
        "credit",
        f"dispute {dispute.id} upheld — step {dispute.step_index} credited {amount_usdc:.7f} USDC "
        f"to the buyer, funded by the platform, not clawed back from agent {dispute.agent_id}"
        + (f" · tx {tx_hash}" if tx_hash else ""),
    )


async def _note_rating_on_workflow(dispute: DisputeRecord, outcome: dispute_rating.RatingOutcome) -> None:
    """Show a landed dispute rating on the workflow it disputes (`_trace_on_workflow`).

    `proof`, the level the settler's own ratings trace at, because this is the
    same kind of evidence: a transaction on the ReputationLedger. It states the
    consequence in plain words — the score, and that an upheld dispute earned
    it — because the credit line before it has just told the buyer the agent
    kept its money, and this is the line that says what the agent lost instead.
    The derived job id is named beside the hash so a reviewer can find the
    rating on Stellar Expert by either.
    """
    await _trace_on_workflow(
        dispute,
        "proof",
        "rating",
        f"reputation → agent {dispute.agent_id} rated {outcome.rating}/100 for upheld dispute {dispute.id}"
        f" on step {dispute.step_index} · dispute job {outcome.job_id_hex} · tx {outcome.tx_hash}",
    )


# ── the dispute rating: the reputation consequence (story 4.04) ─
#
# Everything below runs only once the buyer has been paid, and nothing in it
# may undo that (D3): no call on this path reaches `claim_refund`,
# `release_refund_claim` or `credit_refund`. A rating that fails is a
# consequence that has not landed YET, never a refund to reverse — and unlike
# the refund it can always be retried, because the ledger's replay guard on the
# derived id refuses a second landing, which is the whole of the idempotency
# the refund path needed a durable mutex to buy.


def _derived_id_hex(dispute: DisputeRecord) -> str:
    """The derived job id this dispute is rated under, for a log line only.

    Recomputed for the lines that have no `RatingOutcome` to read it from — an
    attempt that raised, and one never made — and never allowed to raise
    itself, because the derivation's own refusal may be the very failure the
    line reports.
    """
    try:
        return dispute_rating.dispute_job_id(bytes.fromhex(dispute.job_id_hex), dispute.step_index).hex()
    except ValueError:
        return "underivable"


def _log_rating(
    level: int,
    event: str,
    dispute: DisputeRecord,
    derived_hex: str,
    tx_hash: str | None,
    *,
    exc_info: bool = False,
) -> None:
    """One line per rating outcome, carrying every id a reconciliation needs.

    The dispute, the sealed job it disputes, the DERIVED id the rating lives
    under on-chain, the agent it rates and the payer it was written for: with
    those, whoever holds a block explorer can find the rating or prove it is
    absent from this line alone. The hash joins them whenever there is one.
    Nothing secret — all of it is public, and the scorer's key never enters
    this module.
    """
    logger.log(
        level,
        "dispute rating %s: dispute=%s job=%s derived=%s agent=%s payer=%s tx=%s",
        event,
        dispute.id,
        dispute.job_id_hex,
        derived_hex,
        dispute.agent_id,
        dispute.payer,
        tx_hash or "-",
        exc_info=exc_info,
    )


def _tell_observer(observer: RatingObserver, dispute: DisputeRecord, outcome: dispute_rating.RatingOutcome) -> None:
    """Hand the ledger's answer to an observer that asked for it, and survive it.

    By the time there is an answer the credit has landed, and this module's
    rule after that point is that nothing is raised — a paid dispute must never
    read as a failed one. A caller's observer is the caller's code, so a fault
    in it is logged against the dispute and goes no further.
    """
    try:
        observer(outcome)
    except Exception:
        logger.exception(
            "dispute rating observer raised; the rating stands as the ledger answered it: dispute=%s status=%s",
            dispute.id,
            outcome.status,
        )


async def _rate_credited(
    credited: DisputeRecord,
    settlement: SettlementRecord,
    *,
    on_rating: RatingObserver | None = None,
) -> DisputeRecord:
    """Write the rating an upheld dispute earns, and answer with the dispute.

    Only ever called with a dispute that is already `credited`: straight after
    its credit lands, and again on every later `uphold` of it. The five
    answers the ledger can give, and what each one means for THIS dispute:

      - **SUCCESS** — it landed. The hash is recorded as `rating_tx` with
        `rating_confirmed` True, the agent's cached score is invalidated so
        routing sees the rating now rather than one read TTL from now, and the
        workflow is told.
      - **REPLAY, with a `rating_tx` on record** — an earlier attempt of ours
        landed, so this is done: the hash on record is kept and the cache is
        invalidated, because a rating that timed out may have landed since.
        The replay is the ledger vouching for that hash, so a record that did
        not yet say so gets `rating_confirmed` True — the one move from
        unconfirmed to confirmed a rating makes — and one that already does
        is answered unchanged.
      - **REPLAY, with none** — a COLLISION (D4): the ledger holds a rating
        under this dispute's derived id that this dispute has no record of
        writing. Loud, and never read as resolved.
      - **TIMEOUT** — submitted and unconfirmed: it may still land. The
        in-flight hash is recorded at once, so the evidence exists the moment
        the rating does, with `rating_confirmed` False, so a receipt holding
        that hash does not claim a consequence nobody has seen land. The next
        `uphold` settles it — REPLAY if it landed, a fresh SUCCESS that
        replaces the hash if it never did.
      - **FAILED** — nothing landed and nothing is recorded; retryable.

    A rating that cannot even be FORMED — `submit_dispute_rating` raises for a
    settlement with no such step or a job id that will not derive — is none of
    those, and is logged as the records problem it is. Nor is one this
    deployment is not configured to write (`rating_writer.config_gap`), which
    is never submitted at all.

    THE CALLER LEARNS WHETHER THE RATING LANDED FROM THE RECORD, never from an
    exception, and this never raises — short of a cancellation — once it has
    been handed a paid dispute:

      - `credited` WITH a `rating_tx`: the rating was submitted under the
        derived id — and landed when `rating_confirmed` is True, or, after a
        TIMEOUT, may still when it is False;
      - `credited` WITHOUT one: the buyer is paid and the reputation
        consequence is NOT on-chain — a FAILED rating, a collision, a
        deployment not configured to rate, or one that could not be formed or
        recorded. That dispute is not fully resolved, and upholding it again
        retries the rating alone.

    Not an exception, because by now the money has moved: a 5xx would tell
    the caller the adjudication failed when the buyer has in fact been paid.
    Not a second return type either, because it would be a second answer that
    the record contradicts the moment a response is lost — the record is what
    `GET /api/disputes/{id}` serves, what `DisputeResponse` already exposes
    (`status`, `rating_tx`), and what the operator script reads after every
    run. Durable, and one answer. What distinguishes a collision from a
    failure is what an OPERATOR does next, not the caller, so it is the ERROR
    line that names which one it was.
    """
    gap = rating_writer.config_gap()
    if gap is not None:
        # The presence-only gate the settler's own ratings pass
        # (`execution_svc._submit_ratings`). Without it a deployment that
        # cannot sign a rating still submits, the submit raises, and the
        # outcome is a TIMEOUT — "unconfirmed" on every uphold, forever, when
        # the truth is "not configured". So nothing is submitted and the line
        # names the setting. ERROR per dispute rather than the settler's
        # hourly note: an upheld dispute whose agent is never rated breaks the
        # disclosed model's one promise about the agent — that its
        # consequence is reputational — and each one needs finding.
        _log_rating(
            logging.ERROR,
            f"not submitted — {gap.problem}; the credit stands and the agent is NOT rated until it is set"
            " and the dispute is upheld again",
            credited,
            _derived_id_hex(credited),
            None,
        )
        return credited
    try:
        outcome = await dispute_rating.submit_dispute_rating(credited, settlement)
    except Exception:
        # `submit_dispute_rating` turns every answer the CHAIN can give into
        # an outcome — a submit that raised included, as a TIMEOUT — and
        # raises only when the rating cannot be formed from this dispute's
        # records. Those changed under a paid dispute, and no retry mends
        # that, so this says so rather than inviting one. Answered with the
        # record, not re-raised: the credit has landed.
        _log_rating(
            logging.ERROR,
            "could not be formed — the credit stands and no rating is recorded; a retry will not mend this,"
            " the dispute's records need a human",
            credited,
            _derived_id_hex(credited),
            None,
            exc_info=True,
        )
        return credited
    if on_rating is not None:
        _tell_observer(on_rating, credited, outcome)
    try:
        return await _apply_rating(credited, outcome)
    except Exception:
        # In practice only a store write can get here: the one after a
        # SUCCESS or a TIMEOUT, or the confirmation after a REPLAY.
        if outcome.status == "REPLAY":
            # The hash is already on record and only its confirmation was
            # lost, so there is nothing to record by hand: the next uphold is
            # refused as a replay again and writes the confirmation then.
            _log_rating(
                logging.ERROR,
                "was confirmed on-chain but the confirmation could not be recorded on the dispute — its"
                " rating_tx stands; uphold again to record it",
                credited,
                outcome.job_id_hex,
                credited.rating_tx,
                exc_info=True,
            )
            return credited
        # After a SUCCESS or a TIMEOUT the answer and its hash are already in
        # the log. The dispute is answered as the store last held it: paid,
        # and not shown as rated — which a later REPLAY will then report as a
        # collision, so this line is where that one is explained.
        _log_rating(
            logging.ERROR,
            f"was {outcome.status} but could not be recorded on the dispute — record rating_tx by hand",
            credited,
            outcome.job_id_hex,
            outcome.tx_hash,
            exc_info=True,
        )
        return credited


async def _apply_rating(credited: DisputeRecord, outcome: dispute_rating.RatingOutcome) -> DisputeRecord:
    """What one rating outcome means for this dispute — `_rate_credited`'s five.

    Keyed off `credited`'s own `rating_tx` and `rating_confirmed` — the record
    as `uphold` read it — never re-read from the store, so a REPLAY is judged
    against what this dispute had recorded before the attempt that drew it.
    """
    store = get_dispute_store()
    derived = outcome.job_id_hex

    if outcome.status == "SUCCESS":
        # Logged and the cache dropped BEFORE the record is written: the
        # rating is on-chain whatever happens to the store next, so the hash
        # must be in the log and the score fresh even if the write fails.
        _log_rating(logging.INFO, f"landed ({outcome.rating}/100)", credited, derived, outcome.tx_hash)
        reputation_svc.invalidate_rep(credited.agent_id)
        rated = await store.append_status(credited.id, "credited", rating_tx=outcome.tx_hash, rating_confirmed=True)
        await _note_rating_on_workflow(rated, outcome)
        return rated

    if outcome.status == "REPLAY":
        if credited.rating_tx:
            reputation_svc.invalidate_rep(credited.agent_id)
            _log_rating(logging.INFO, "already on-chain — kept", credited, derived, credited.rating_tx)
            if credited.rating_confirmed:
                # Already confirmed: a second row would say nothing new and
                # would move `updated_at` for a dispute nothing happened to.
                return credited
            # A timeout that landed after its poll gave up, or a rating recorded
            # before 4.06 kept whether it landed: either way the ledger has now
            # vouched for the hash on record, and only now may the receipt say
            # the agent was rated.
            return await store.append_status(credited.id, "credited", rating_confirmed=True)
        _log_rating(
            logging.ERROR,
            "COLLISION — the ledger already holds a rating under this dispute's derived id and this"
            " dispute records none, so its reputation consequence did NOT land; the credit stands."
            " Look the derived id up on-chain: if it is this dispute's own unrecorded attempt (a"
            " timeout with no hash, or a record write that failed), record that hash as rating_tx",
            credited,
            derived,
            None,
        )
        return credited

    if outcome.status == "TIMEOUT":
        # Logged before it is recorded, for the reason SUCCESS is.
        _log_rating(
            logging.ERROR,
            "unconfirmed — it may still land; uphold again to settle it",
            credited,
            derived,
            outcome.tx_hash,
        )
        if outcome.tx_hash:
            # Evidence, and explicitly NOT confirmation: the hash is the
            # rating's the moment it lands, but until the ledger vouches for it
            # the receipt must not say the agent was rated.
            return await store.append_status(credited.id, "credited", rating_tx=outcome.tx_hash, rating_confirmed=False)
        return credited

    # FAILED — and, deliberately, anything else: for a rating the safe
    # default is "not landed, retry", because the replay guard makes a retry
    # that double-rates impossible. Any hash an earlier TIMEOUT left on the
    # record stays, since this attempt says nothing about that one.
    _log_rating(
        logging.ERROR,
        f"failed ({outcome.status}) — nothing landed; uphold again to retry",
        credited,
        derived,
        outcome.tx_hash,
    )
    return credited


async def _retry_rating(credited: DisputeRecord, *, on_rating: RatingObserver | None = None) -> DisputeRecord:
    """Re-attempt the rating of a dispute already `credited` — THE RATING ONLY.

    The repeat-uphold half of D3: it reads the settlement the rating is
    weighted from and hands over to `_rate_credited`, and it goes nowhere near
    the claim or the transfer. `tests/test_adjudication.py` booby-traps both
    to hold it to that.

    A settlement that is no longer on record leaves no step price to weight a
    rating with, so nothing is submitted. ERROR when that leaves the dispute
    with no rating on record — the consequence has not landed and cannot be
    retried from here — and WARNING when one is on record already, because
    all that is skipped then is the re-check.
    """
    settlement = await get_dispute_store().get_settlement(credited.job_id_hex)
    if settlement is None:
        _log_rating(
            logging.WARNING if credited.rating_tx else logging.ERROR,
            "not re-attempted — the settlement that weights it is no longer on record",
            credited,
            _derived_id_hex(credited),
            credited.rating_tx,
        )
        return credited
    return await _rate_credited(credited, settlement, on_rating=on_rating)


async def uphold(dispute_id: str, *, on_rating: RatingObserver | None = None) -> DisputeRecord:
    """Adjudicate a dispute in the BUYER's favour, pay the credit, and rate the agent.

    THE ORDER BELOW IS THE STORY. Each step exists to close one way of paying a
    buyer twice, or of leaving one who is owed unable ever to be paid, so none
    of them may be reordered for tidiness:

      0. **The master switch** — `DISPUTE_REFUNDS_ENABLED` is checked before
         the store is even read, so the answer cannot depend on anything a
         dispute happens to say (`refunds_disabled`, 503). It is enforced HERE
         as well as in the route's `require_adjudicator` (D1) because the route
         is one of two doors: an operator script that imports this service
         credits a buyer without passing through FastAPI at all, and a switch
         that only one door honours is not a switch.
      1. **Load it** — an id nobody issued is `unknown_dispute` (404).
      2. **Already `credited`** — sign NO transfer: the refund is answered
         with the `refund_tx` it already carries. This is the retry acceptance
         criterion, and it sits ABOVE the claim on purpose: an adjudicator who
         double-clicks, a proxy that retries a 502, a queue that redelivers —
         all of them arrive here and none of them may depend on `claim_refund`
         to be told no. (The claim would also say no, because a credited
         dispute is not `upheld`. Two independent answers to "has this already
         been paid" is the point, not redundancy to trim.) What IS retried,
         every time, is the RATING and only the rating (step 10, D3) — safe
         because the ledger's replay guard makes a second landing impossible.
      3. **`crediting`** — a transfer for this dispute is ON THE NETWORK and
         nobody knows whether it landed (D3). Refuse with `refund_in_flight`
         and never pay: the only two ways out are the network confirming it or
         a human reconciling it, and a second transfer is neither.
      4. **`rejected`** — adjudicated against the claim, and terminal
         (`dispute_rejected`). A rejected dispute is never payable.
      5. **Configured to pay** — the presence-only gate on the settler's key
         and the asset SAC (`refund_svc.config_gap`), the twin of the one
         `_rate_credited` applies to the rating. It sits ABOVE the claim
         because a deployment that cannot sign must claim nothing: the key is
         read inside the transfer, where it raises before any submission, and
         a raise out of a transfer cannot be told from one that may have
         landed — so without this the claim is taken, the dispute parks in
         `crediting`, and only a database edit ever frees it
         (`refunds_not_configured`, 503).
      6. **`open` → `upheld`** — the adjudication itself, recorded BEFORE the
         claim because `claim_refund` only ever claims an `upheld` dispute.
         An `upheld` one skips straight to the claim, which is what makes a
         dispute left upheld by a FAILED transfer payable again.
      7. **Claim it** (D2) — the lock, taken before anything is signed and
         never a read-then-write. `None` means somebody else holds it, so the
         current record is returned rather than a second transfer signed.
      8. **Compute and cap the amount** (D4, D5) — `refund_svc` bounds it by
         what actually settled and refuses above the ceiling. Every
         `RefundRefused` is raised BEFORE the settler's key is touched, from
         either call, so the claim is RELEASED — nothing was signed and the
         buyer may still be owed — and the refusal is re-raised as a
         `DisputeError` in this module's vocabulary.
      9. **Transfer**, and treat its three answers as three different facts:
         SUCCESS records `credited` with the hash; FAILED definitively moved
         nothing, so the claim is released and the dispute is left `upheld` and
         payable; TIMEOUT **keeps the claim**, leaves the dispute `crediting`
         with the in-flight hash recorded, logs ERROR and refuses. Never a
         retry, never a release (D3).
     10. **Rate the agent** (story 4.04) — only after the credit has landed
         AND been recorded, so no rating ever exists for a dispute the buyer
         was not paid for. See `_rate_credited` for its five outcomes. A
         rating that does not land NEVER reverses or re-touches the refund:
         the dispute stays `credited` and its `rating_tx` stays empty.

    The return value is the dispute as the store holds it, and it is also how
    a caller learns whether the reputation consequence landed: `credited` with
    a `rating_tx` and `rating_confirmed` True has been rated; with
    `rating_confirmed` False the rating timed out and may yet land, and the
    next uphold settles which; and `credited` WITHOUT a `rating_tx` is paid but
    NOT fully resolved — uphold it again to retry the rating alone. A rating
    failure is never raised: by then the buyer has been paid, and an exception
    would say otherwise.

    The one window that remains is between a SUCCESS and the `append_status`
    that records it: if the store is unreachable at that instant the money has
    moved and the dispute stays `crediting` with its claim held. That is the
    safe side of the trade — the claim blocks a second payment — and the
    failure is logged HERE at ERROR with the dispute, the job, the payer, the
    amount and the hash, which is everything a reconciliation starts from.
    `refund_svc` logs the landed credit too, but at INFO, which is not a level
    anyone is watching: a credit that landed and could not be recorded is the
    highest-stakes line this service writes, so it writes its own.

    `on_rating`, when given, is handed the ledger's answer to the rating the
    moment there is one — at most once per call, and never when no rating was
    submitted. It exists for the operator tool, which must tell a human whether
    the reputation consequence actually landed, and the record cannot say that
    on its own. The HTTP route passes nothing, so what it answers stays exactly
    what a later `GET` of the dispute will answer.
    """
    if not settings.dispute_refunds_enabled:
        # Fails closed, and first: nothing below this line may run on a
        # deployment whose operator has not switched the refund path on, and
        # that must hold however the service was reached.
        logger.warning("adjudication refused: dispute=%s reason=refunds_disabled", dispute_id)
        raise DisputeError(
            "refunds_disabled",
            "dispute refunds are switched off on this deployment",
            503,
        )

    store = get_dispute_store()
    dispute = await _load_for_adjudication(dispute_id)

    if dispute.status == "credited":
        # Not a refusal: the adjudicator asked for this dispute to be credited
        # and it is, so they are answered with the credit — same record, same
        # refund hash, NO second transfer. What is retried is the RATING and
        # only the rating (D3), on every repeat: one that already landed is
        # refused as a replay at simulation, costing nothing on-chain, and one
        # that never did is written now.
        logger.info(
            "dispute %s is already credited — tx %s, no transfer signed; re-attempting its rating only",
            dispute.id,
            dispute.refund_tx,
        )
        return await _retry_rating(dispute, on_rating=on_rating)

    if dispute.status == "crediting":
        raise _refuse_credit(
            dispute,
            "refund_in_flight",
            409,
            "a credit for this dispute is already on the network and its outcome is unknown —"
            " it must be reconciled by hand, never retried",
            amount_usdc=dispute.creditable_usdc,
            tx_hash=dispute.refund_tx,
            level=logging.ERROR,
        )

    if dispute.status == "rejected":
        raise _refuse_credit(
            dispute,
            "dispute_rejected",
            409,
            "this dispute was rejected, so it can never be credited",
            amount_usdc=dispute.creditable_usdc,
        )

    gap = refund_svc.config_gap()
    if gap is not None:
        # Two lines, because they answer two people. This one names the
        # setting, for the operator who has to set it; `_refuse_credit`'s
        # names the dispute, job, payer and amount, which is the money-path
        # record every refusal here leaves. ERROR on both: nothing moved and
        # nothing is stuck, but an upheld dispute nobody is configured to pay
        # is a buyer waiting on a human.
        logger.error(
            "dispute %s cannot be credited — %s; nothing was claimed and nothing was signed (job %s, payer %s)",
            dispute.id,
            gap.problem,
            dispute.job_id_hex,
            dispute.payer,
        )
        raise _refuse_credit(
            dispute,
            "refunds_not_configured",
            503,
            f"this deployment cannot sign a credit: {gap.reason}",
            amount_usdc=dispute.creditable_usdc,
            level=logging.ERROR,
        )

    if dispute.status == "open":
        dispute = await store.append_status(dispute_id, "upheld")

    claimed = await store.claim_refund(dispute_id)
    if claimed is None:
        # Another caller took the claim between the transition above and this
        # line. They are paying, or have just paid, so this one returns what
        # the dispute now says instead of signing a second transfer. Re-read
        # rather than return `dispute`: the winner has already moved it on.
        current = await store.get_dispute(dispute_id) or dispute
        logger.info("dispute %s is already claimed (%s) — nothing signed here", current.id, current.status)
        return current

    settlement = await store.get_settlement(claimed.job_id_hex)
    if settlement is None:
        # The amount is bounded by what the settlement says actually moved
        # (D4), so without the settlement there is no number that is safe to
        # pay. ERROR, not WARNING: a buyer with an upheld dispute and no
        # settlement to price it from can only be paid by a human, and the
        # claim is handed back so that a human still can.
        await store.release_refund_claim(dispute_id)
        raise _refuse_credit(
            claimed,
            "settlement_missing",
            409,
            "the settlement this dispute was judged against is no longer on record,"
            " so the credit cannot be bounded by what was actually charged",
            amount_usdc=claimed.creditable_usdc,
            level=logging.ERROR,
        )

    try:
        amount_usdc = refund_svc.creditable_for(settlement, claimed, settings.dispute_credited_fraction)
        outcome = await refund_svc.credit_refund(claimed, amount_usdc)
    except refund_svc.RefundRefused as refused:
        # Both refusals — the cap and "nothing to credit" — are raised before
        # `execute_refund` is called, from `creditable_for` and again from the
        # transfer wrapper's own re-check, so NOTHING WAS SIGNED on either
        # path. That is what makes releasing the claim correct here and wrong
        # after a timeout. `refund_svc` has already logged the numbers, so
        # this only re-raises in the vocabulary the API answers with.
        await store.release_refund_claim(dispute_id)
        raise _refuse_credit(claimed, refused.code, 409, refused.message) from None

    if outcome.status == "SUCCESS":
        # The amount the transfer MOVED, never `creditable_usdc`: that is the
        # promise frozen at opening, and D4 bounds the payment below it by the
        # step price at today's fraction and by what the charge settled. The
        # receipt prints this beside the refund hash, so it must be the number
        # the hash proves.
        try:
            credited = await store.append_status(
                dispute_id, "credited", refund_tx=outcome.tx_hash, credited_usdc=outcome.amount_usdc
            )
        except Exception:
            # The money has MOVED and nothing else would say so where anyone
            # would see it: `credit_refund`'s SUCCESS line is INFO, and an
            # exception let out from here reaches the caller as a bare 500
            # carrying no dispute, job, payer, amount or hash at all. Logged
            # before it is re-raised, because re-raising is still right: the
            # claim stays held and the dispute stays `crediting`, which is
            # exactly what a credit that landed and could not be recorded is.
            logger.error(
                "dispute %s: %.7f USDC LANDED as tx %s and the credit could NOT be recorded — the dispute"
                " stays crediting with its claim held; record it by hand (job %s, payer %s)",
                dispute_id,
                outcome.amount_usdc,
                outcome.tx_hash,
                claimed.job_id_hex,
                claimed.payer,
                exc_info=True,
            )
            raise
        await _note_credit_on_workflow(credited, outcome.amount_usdc, outcome.tx_hash)
        # Only now, with the credit landed AND recorded, is the agent rated —
        # and against the settlement the credit was just bounded by, so the
        # rating is weighted by the same step it refunded.
        return await _rate_credited(credited, settlement, on_rating=on_rating)

    if outcome.status == "FAILED":
        # The ledger rejected it, which is the ONE answer that says no funds
        # moved. The buyer is still owed, so the claim goes back and the
        # dispute is left `upheld` — a second uphold will claim it and try
        # again, which is the whole reason this release exists.
        await store.release_refund_claim(dispute_id)
        raise _refuse_credit(
            claimed,
            "refund_failed",
            502,
            "the credit transfer did not settle, so nothing was paid — the dispute is still upheld"
            " and can be credited again",
            amount_usdc=outcome.amount_usdc,
            tx_hash=outcome.tx_hash,
            level=logging.ERROR,
        )

    # TIMEOUT, and anything the wrapper could not classify, which it maps here
    # for the same reason: a submission whose fate is unknown MAY STILL LAND.
    # The claim is NOT released and the dispute stays `crediting` — releasing
    # it would unlock a retry that credits the buyer a second time the moment
    # the first submission settles. The in-flight hash is recorded on the
    # dispute so the reconciliation starts from the record rather than from a
    # log search.
    await store.append_status(dispute_id, "crediting", refund_tx=outcome.tx_hash)
    raise _refuse_credit(
        claimed,
        "refund_unconfirmed",
        504,
        "the credit was submitted and its outcome is unknown — it may still land, so it must be"
        " reconciled by hand and never retried",
        amount_usdc=outcome.amount_usdc,
        tx_hash=outcome.tx_hash,
        level=logging.ERROR,
    )
