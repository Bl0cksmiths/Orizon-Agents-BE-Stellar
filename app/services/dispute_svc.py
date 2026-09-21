"""When a buyer may dispute a settled step, what it records, and how it is paid.

Stories 4.02 and 4.03, ADR 0002. This module is the gate in front of the money:
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
performs off-chain (ADR 0002's disclosed trust model); 4.04 writes the dispute
rating. The credit is FUNDED BY THE PLATFORM and is never clawed back from the
agent — the deployed escrow takes no custody, so there is nothing to reverse —
and every artifact a buyer can see has to say so (SOW §3.8).

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
from datetime import datetime, timezone

from ..agents.workers.prompt_safety import sanitize_untrusted
from ..config import settings
from ..schemas import TraceLine
from ..state import state
from ..trace_bus import bus
from . import external_binding as eb
from . import refund_svc
from .dispute_store import (
    DisputeRecord,
    DuplicateDisputeError,
    SettlementRecord,
    get_dispute_store,
    new_dispute_id,
)

logger = logging.getLogger(__name__)

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
    # `kind`. So when story 4.04 records this dispute on-chain it must write the
    # rating under `refund_svc.dispute_job_id(job_id)` — the derived id from
    # ADR 0002 — or the submission comes back `Error::Replay` and the dispute
    # silently never lands. Nothing in THIS story writes on-chain at all.
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


async def reject(dispute_id: str, *, note: str | None = None) -> DisputeRecord:
    """Adjudicate a dispute AGAINST the claim, from `open` and from nowhere else.

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

    `note` is the adjudicator's reason. `DisputeRecord` has no field for it —
    the record carries the BUYER's evidence, and inventing a place for the
    platform's own commentary inside it is not this story's to do — so it is
    logged with the decision and cleaned exactly the way a buyer's reason is,
    because free text that reaches a log is free text either way.
    """
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
    rejected = await get_dispute_store().append_status(dispute_id, "rejected")
    logger.info(
        "dispute rejected: id=%s job=%s step=%s payer=%s note=%s",
        rejected.id,
        rejected.job_id_hex,
        rejected.step_index,
        rejected.payer,
        sanitize_untrusted(note, max_chars=MAX_REASON_CHARS) if note else "-",
    )
    return rejected


async def _note_credit_on_workflow(dispute: DisputeRecord, amount_usdc: float, tx_hash: str | None) -> None:
    """Best effort: show a landed credit on the workflow it came out of.

    THE DURABLE RECORD OF A REFUND IS THE DISPUTE, NOT THIS LINE. `app/state.py`
    keeps the newest 200 tasks and drops each one's traces with it, while a
    dispute window is 24 hours wide — so by the time one is adjudicated the
    workflow it disputes has usually been evicted, and this is decoration on
    the ones a console still has on screen. Nothing reads it back, nothing
    reconciles against it, and it is emitted after the store already holds the
    credit so it can never be the reason a paid dispute looks unpaid.

    Which is exactly why it is GUARDED on `state.tasks` rather than simply
    appended. `state.append_trace` is `traces.setdefault(task_id, []).append(line)`,
    and for an evicted task that does not merely write where nobody looks: it
    RECREATES a `traces` entry whose task is gone, and eviction only ever
    removes traces alongside a task still in `task_order`, so nothing will
    remove it again. A dispute resolved hours later would leak one list per
    refund for the life of the process, invisibly. Present task only, and never
    `setdefault` on an absent one.

    `state` and `bus` directly, not `execution_svc._emit`: that helper is keyed
    on the run's `time.monotonic()` start, which died with the request that
    held it, so it could not be reused here even if the import were free. And
    it would not be free — the module holding the dispute RULES would come to
    depend on the module that RUNS workflows, in the one direction ADR 0002
    keeps clear, for the sake of four lines. `TraceLine` and the bus are the
    whole of what the two actually share, so those are the whole of what this
    imports.
    """
    task = state.tasks.get(dispute.task_id)
    if task is None:
        return
    # The same elapsed-since-the-run-started clock every other line on this
    # task carries, so a credit sorts where it happened rather than at 00.000.
    elapsed = max(time.time() - task.started_at, 0.0)
    seconds, millis = divmod(int(elapsed * 1000), 1000)
    # `cost` because a refund is money, and the wording is the SOW §3.8
    # standard rather than a turn of phrase: the platform FUNDS this credit out
    # of its own wallet, and the disputed agent keeps what it was paid. This is
    # the only message about a refund the buyer ever sees, so it is the one
    # that has to say so.
    line = TraceLine(
        t=f"{seconds:02d}.{millis:03d}",
        level="cost",
        msg=(
            f"dispute {dispute.id} upheld — step {dispute.step_index} credited {amount_usdc:.7f} USDC "
            f"to the buyer, funded by the platform, not clawed back from agent {dispute.agent_id}"
            + (f" · tx {tx_hash}" if tx_hash else "")
        ),
    )
    try:
        state.append_trace(dispute.task_id, line)
        await bus.publish(dispute.task_id, line)
    except Exception:
        # The transfer has already settled and the dispute already reads
        # `credited` by the time this runs. Letting a cosmetic line raise out
        # of `uphold` would answer a successful payout with a 500 and invite
        # the one retry this whole path exists to make safe, so it is logged
        # and swallowed instead.
        logger.warning("could not trace the credit for dispute %s on task %s", dispute.id, dispute.task_id)


async def uphold(dispute_id: str) -> DisputeRecord:
    """Adjudicate a dispute in the BUYER's favour and pay the settler-funded credit.

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
      2. **Already `credited`** — return the record UNCHANGED, with the
         `refund_tx` it already carries, and sign nothing. This is the retry
         acceptance criterion, and it sits ABOVE the claim on purpose: an
         adjudicator who double-clicks, a proxy that retries a 502, a queue
         that redelivers — all of them arrive here and none of them may depend
         on `claim_refund` to be told no. (The claim would also say no, because
         a credited dispute is not `upheld`. Two independent answers to "has
         this already been paid" is the point, not redundancy to trim.)
      3. **`crediting`** — a transfer for this dispute is ON THE NETWORK and
         nobody knows whether it landed (D3). Refuse with `refund_in_flight`
         and never pay: the only two ways out are the network confirming it or
         a human reconciling it, and a second transfer is neither.
      4. **`rejected`** — adjudicated against the claim, and terminal
         (`dispute_rejected`). A rejected dispute is never payable.
      5. **`open` → `upheld`** — the adjudication itself, recorded BEFORE the
         claim because `claim_refund` only ever claims an `upheld` dispute.
         An `upheld` one skips straight to the claim, which is what makes a
         dispute left upheld by a FAILED transfer payable again.
      6. **Claim it** (D2) — the lock, taken before anything is signed and
         never a read-then-write. `None` means somebody else holds it, so the
         current record is returned rather than a second transfer signed.
      7. **Compute and cap the amount** (D4, D5) — `refund_svc` bounds it by
         what actually settled and refuses above the ceiling. Every
         `RefundRefused` is raised BEFORE the settler's key is touched, from
         either call, so the claim is RELEASED — nothing was signed and the
         buyer may still be owed — and the refusal is re-raised as a
         `DisputeError` in this module's vocabulary.
      8. **Transfer**, and treat its three answers as three different facts:
         SUCCESS records `credited` with the hash; FAILED definitively moved
         nothing, so the claim is released and the dispute is left `upheld` and
         payable; TIMEOUT **keeps the claim**, leaves the dispute `crediting`
         with the in-flight hash recorded, logs ERROR and refuses. Never a
         retry, never a release (D3).

    The one window that remains is between a SUCCESS and the `append_status`
    that records it: if the store is unreachable at that instant the money has
    moved and the dispute stays `crediting` with its claim held. That is the
    safe side of the trade — the claim blocks a second payment, and
    `refund_svc` has already logged the hash for reconciliation.
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
        # transaction hash, no second transfer.
        logger.info("dispute %s is already credited — tx %s, nothing signed", dispute.id, dispute.refund_tx)
        return dispute

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
        credited = await store.append_status(dispute_id, "credited", refund_tx=outcome.tx_hash)
        await _note_credit_on_workflow(credited, outcome.amount_usdc, outcome.tx_hash)
        return credited

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
