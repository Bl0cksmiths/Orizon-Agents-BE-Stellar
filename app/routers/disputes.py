"""The dispute window's HTTP surface — story 4.02 (ADR 0002).

A buyer who paid for a step that did not deliver what it promised has 24 hours
from settlement to say so. Four routes are the whole of how they say it: mint a
challenge, sign it with the wallet that paid, post the dispute, and read back
what was raised on a task. Two more — added by story 4.03 — are how the
platform answers: uphold, and credit; or reject, and say so. And two more —
D-067 — let the payer READ what was said, from any tab and after any restart:
sign a read challenge, get a grant, send it with the two reads. The grant buys
those reads' free text and nothing else.

**On the buyer's four, the wallet signature IS the credential** — no API key,
no account — for `routers/binding.py`'s reason: a shared secret cannot express
"this caller is *this* job's payer", and gating these routes on
`require_api_key` would be a no-op on the demo, where API_KEY is unset. It
would also be the wrong guard entirely: the operator holds that key, and the
operator is the party a dispute is raised *against*.

**The adjudication pair is the mirror image**, and takes the opposite guard for
the same reason. The caller there is not the buyer but the house, answering a
claim made against itself and spending its own settler balance to do it. No
wallet signature can express that, so `require_adjudicator` is the credential —
and unlike `require_api_key` it FAILS CLOSED, because an open route that pays
out is a drain rather than a demo. See its docstring for the whole argument.

Every rule — who may dispute, whether the window is still open, whether the
step was settled at all — lives in `services/dispute_svc.py`. This module
validates shapes at the edge, calls one service function, and maps its refusal
onto a status and a stable code. Nothing here decides anything, because a rule
enforced in a handler is a rule the next handler forgets.

`POST /disputes` returns **200**, not 201: no route in this API returns 201, and
a `duplicate_dispute` answers with the dispute that already exists rather than
minting a second one — so the location the 201 would advertise is not always
new. That duplicate answer is why `_duplicate_envelope` exists; see it for why
a 409 here carries a body the generic error envelope has no room for.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..schemas import SettlementState
from ..security import (
    CodedHTTPException,
    ErrorEnvelope,
    KeyedRateLimiter,
    client_identity,
    request_id_var,
    require_adjudicator,
)
from ..services import dispute_read, dispute_svc, refund_svc, task_persistence
from ..services.dispute_store import DisputeRecord, DisputeStatus, SettlementRecord, SettlementStep
from ..services.external_binding import dispute_read_message
from ..state import state
from ..task_auth import TaskReadProof, task_read_proof

logger = logging.getLogger(__name__)

router = APIRouter(tags=["disputes"])

# A 16-byte job id, hex — the same shape `ChargeReq` and `SealReq` take, so a
# job id that could never have been charged is refused before any lookup.
_JOB_ID_PATTERN = r"^[0-9a-fA-F]{32}$"

# A Stellar G-address, as `SealReq.orchestrator` spells it. The payer is
# claimed, not trusted: the signature is what proves it, and this only bounds
# what reaches the verifier.
_PAYER_PATTERN = r"^G[A-Z2-7]{55}$"

# A task id, as `execution_svc` mints it (`tsk_` and hex) and as every fixture
# spells one. Bounded to the path parameter's 128 and to characters that cannot
# carry a `:` or a newline into a message a wallet is about to show and sign.
_TASK_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
_MAX_TASK_ID_CHARS = 128

# Twice the 32-entry cap `POST /api/stellar/server/seal` puts on a workflow's
# agents and receipts, so a plan that grows is not refused *here* first. The
# real check is that the settlement record actually has such a step, which only
# the service can make.
_MAX_STEP_INDEX = 63


# Per-client budget for the dispute challenge mint, on top of the global
# limiter. The service refuses a mint for any step that could not be disputed,
# which leaves only steps inside their window — but those are still public, and
# each mint holds one of the `dispute` purpose's 200 slots for five minutes. So
# one client may spend only a slice of it. Counted before the service is asked,
# so a refused mint costs the caller as much as an admitted one: a stranger
# probing for slots pays for every probe.
_challenge_limiter = KeyedRateLimiter(lambda: settings.dispute_challenge_rate_limit_per_minute)


class DisputeChallengeReq(BaseModel):
    """What the wallet is about to sign is derived from these two, so both are
    supplied when the challenge is minted rather than first seen at open time."""

    job_id_hex: str = Field(..., pattern=_JOB_ID_PATTERN)
    step_index: int = Field(..., ge=0, le=_MAX_STEP_INDEX)


# How coarsely a dispute challenge's expiry is reported, in seconds.
#
# `issue_challenge` is idempotent inside the window — a live challenge comes
# back AS IS, which is what stops a flood cancelling the nonce a buyer is
# signing — so anyone who knows a (job, step) could mint against it and read
# the REMAINING TTL off the answer. That is `expires_at` minus a five-minute
# constant: the moment somebody started disputing that step, to the second,
# answered to an anonymous caller. Which steps of which workflows are being
# disputed right now, and when each one began, is not something this route is
# asked and not something it should tell.
#
# Quantised on the ABSOLUTE expiry rather than on the remaining time, so the
# answer does not move with how long the handler took — two callers a
# millisecond apart get the same value, which is the whole point of reporting
# a coarse one. Rounded DOWN, so the buyer is never told they have longer to
# sign than they do; the cost is up to 59 seconds of a 300-second window, and
# a challenge that lapses is re-minted for free.
_EXPIRY_GRAIN_SECONDS = 60


def _coarse_expiry(expires_at: float) -> float:
    """`expires_at` floored to `_EXPIRY_GRAIN_SECONDS`. See that constant."""
    return float(int(expires_at // _EXPIRY_GRAIN_SECONDS) * _EXPIRY_GRAIN_SECONDS)


class DisputeChallengeResponse(BaseModel):
    # The exact string the wallet must sign, returned rather than assembled
    # client-side so the format can version without shipping a new frontend —
    # `BindChallengeResponse`'s reasoning, and the same trade.
    message: str
    nonce: str
    # COARSE, and never later than the real expiry — see `_coarse_expiry`. The
    # nonce this accompanies is exact; only the clock is blurred.
    expires_at: float


class DisputeReadChallengeReq(BaseModel):
    """Which task's disputes the payer is about to prove they may read (D-067)."""

    task_id: str = Field(..., pattern=_TASK_ID_PATTERN)


class DisputeReadChallengeResponse(BaseModel):
    # Field order is the frozen contract's. `message` is always
    # `orizon-dispute-read:v1:<task_id>:<nonce>`, returned whole for
    # `DisputeChallengeResponse`'s reason.
    nonce: str
    message: str
    # COARSE, for `_coarse_expiry`'s reason: the mint is idempotent inside the
    # window, so an exact expiry would tell anyone who can name a task when its
    # payer last started reading their disputes.
    expires_at: float


class DisputeReadGrantReq(BaseModel):
    """The payer's signature over the read challenge. No `payer` field: the
    signer is checked against the settlement's payer and nobody else, so there
    is nothing for the caller to claim."""

    task_id: str = Field(..., pattern=_TASK_ID_PATTERN)
    nonce: str = Field(..., min_length=1, max_length=128)
    # `OpenDisputeReq.signature_b64`'s bound, for its reason.
    signature_b64: str = Field(..., min_length=1, max_length=256, description="base64 ed25519 signature")


class DisputeReadGrantResponse(BaseModel):
    # Opaque. Send it back as `X-Dispute-Read-Grant` on the two dispute reads;
    # it buys their free text and nothing else.
    grant: str
    # Exact, and never more than an hour away.
    expires_at: float


class OpenDisputeReq(BaseModel):
    job_id_hex: str = Field(..., pattern=_JOB_ID_PATTERN)
    step_index: int = Field(..., ge=0, le=_MAX_STEP_INDEX)
    # Bounded by the SERVICE, not here (D-061). A pydantic bound answered an
    # over-long reason with the generic `validation_error`, echoed the whole
    # reason back in `detail[].input`, and gave an empty reason a different
    # code from a blank one. `dispute_svc._require_reason` is the one judge:
    # empty, invisible or longer than MAX_REASON_CHARS once cleaned is 422
    # `reason_invalid`, whose message names the limit and quotes nothing. It is
    # the service's FIRST check, before the settlement is read or the signature
    # verified, so a bad reason still costs nothing — and the body limit
    # (`BodyLimitMiddleware`) still bounds what can arrive at all.
    #
    # Nothing past the bound is ever trimmed, and no word is rewritten: the
    # service refuses rather than cuts, and cleans for a reader, not for a
    # prompt fence (D-062) — so what the buyer sent is what is stored, less
    # control characters. The bound is still published in the schema, so a
    # generated client can check it before sending.
    reason: str = Field(
        ...,
        json_schema_extra={"minLength": 1, "maxLength": dispute_svc.MAX_REASON_CHARS},
        description=(
            "What was wrong with the step. 1 to MAX_REASON_CHARS characters once control characters are "
            "removed, at least one visible; anything else is 422 `reason_invalid`."
        ),
    )
    payer: str = Field(..., pattern=_PAYER_PATTERN)
    nonce: str = Field(..., min_length=1, max_length=128)
    # Upper bound only, exactly as `BindReq.signature` has it: a lower bound
    # here would be answered as the generic `validation_error`, which is the one
    # code the frontend cannot map to an inline field error — so the signature's
    # *shape* is settled by the verifier, which answers the stable
    # `signature_malformed`. 256 chars still bounds the decode at ~3x an
    # ed25519 signature's 88, so nothing useful is truncated.
    signature_b64: str = Field(..., min_length=1, max_length=256, description="base64 ed25519 signature")


class RejectDisputeReq(BaseModel):
    """The adjudicator's word on why a dispute was not upheld — which the buyer reads.

    REQUIRED, because it is the buyer's answer: it comes back on the dispute
    as `rejection_reason`, and a rejection with no explanation is worse than
    no dispute system at all. So a body with no note, a null note or an empty
    one is the field-level `validation_error` here, before anything is read.
    The edge settles only the shape; what cleaning leaves of the text is the
    service's to judge, since only it cleans — a note of whitespace or control
    characters passes this bound, cleans to nothing, and is refused there as
    `rejection_reason_required`.

    Bounded at the buyer's reason's number — one paragraph — because it is
    the same kind of thing from the other side of the table, and a rejection
    that outgrew the complaint it answers would be the one free-text field in
    this surface nobody had sized. Bounded HERE, unlike the reason, because
    the caller is the operator holding the key rather than a buyer, so the
    generic field-level 422 is an answer they can read. `dispute_svc.reject`
    refuses a note past the same MAX_REASON_CHARS (`rejection_reason_too_long`)
    rather than cutting it, so no bound anywhere shows the buyer an
    adjudicator's reason cut short.
    """

    note: str = Field(
        ...,
        min_length=1,
        max_length=dispute_svc.MAX_REASON_CHARS,
        description=(
            "Why the dispute was rejected. SHOWN TO THE BUYER as the dispute's "
            "`rejection_reason`, and readable by anyone who can read the task's "
            "disputes — write it for the buyer."
        ),
    )


class DisputeResponse(BaseModel):
    """One dispute, as the console and the buyer's client read it.

    A flat mirror of `dispute_store.DisputeRecord` rather than the record
    itself: the record is a storage shape that 4.03 and 4.04 will add columns
    to, and returning it directly would publish each of those as API the moment
    it landed.
    """

    id: str
    job_id_hex: str
    task_id: str
    step_index: int
    agent_id: str
    payer: str
    reason: str
    status: DisputeStatus
    # What the step cost and what an upheld dispute credits back under the
    # policy in force when it was opened — both frozen at opening time, so the
    # buyer can be shown a number that will not move under them.
    charged_usdc: float
    creditable_usdc: float
    opened_at: float
    resolved_at: float | None = None
    refund_tx: str | None = None
    rating_tx: str | None = None
    # What the refund ACTUALLY transferred — the figure the receipt prints
    # beside the `refund_tx` link. Not `creditable_usdc`: that is the ceiling
    # frozen at opening, and the payout is bounded again when it is made, by
    # the fraction then in force and by what the charge moved, so the two can
    # differ. A receipt that showed the promise next to an explorer page
    # showing another sum would contradict its own evidence. Null until the
    # dispute is credited, and for every dispute credited before 4.06, where
    # the honest answer is "not recorded" rather than the promise passed off
    # as the payout.
    credited_usdc: float | None = None
    # When the dispute last changed state, in epoch seconds on this server's
    # clock. `resolved_at` is stamped once, at the first verdict, so a refund
    # that timed out and was reconciled hours later would otherwise show the
    # buyer the time of a step they are no longer looking at. Null only for a
    # record written before 4.06.
    updated_at: float | None = None
    # Whether `rating_tx` is known to have LANDED, which the hash alone cannot
    # say: it is recorded on a timeout as well as on a success, so a receipt
    # that read "hash present" as "the agent was rated" could claim a
    # consequence that never happened. True once the ledger has vouched for
    # it, false while it is only in flight, null when no rating was submitted
    # or the record predates 4.06 — so null means "not known", never "no".
    rating_confirmed: bool | None = None
    # Why the adjudicator rejected the claim, for the buyer to read: a
    # rejection with no explanation is worse than no dispute system at all.
    # It is the record's `note` under exactly one condition — the status is
    # `rejected` — and null under every other, so a note recorded at any other
    # point in a dispute's life is never published by accident, and never
    # under a second name. Null, too, for a rejection with no note, which only
    # a record from before the note was required can be.
    #
    # WITHHELD from a caller who has not proved they may read this task, along
    # with `reason` above — see `of`.
    rejection_reason: str | None = None
    # True exactly when `reason` and `rejection_reason` were withheld from THIS
    # caller (D-067, D-068). Without it an empty `reason` and a null
    # `rejection_reason` were the only tell, and a null rejection reason also
    # means "not rejected" — so a client could not say "sign to read why"
    # without re-deriving the server's rule. Always present, never null.
    reason_withheld: bool = False

    @classmethod
    def of(cls, record: DisputeRecord, *, free_text: bool) -> DisputeResponse:
        """Project a stored record onto the wire shape.

        `free_text` is whether the two HUMAN-WRITTEN fields go out: the
        buyer's `reason` and, on a rejection, the adjudicator's
        `rejection_reason`. Everything else on this shape is a fact about
        money or state — amounts, statuses, transaction hashes, timestamps —
        and those are on-chain or derivable from what is, so they go to
        whoever the route admits.

        The free text is not. It is the buyer describing, in their own words,
        what they believe went wrong with work they paid for, and the
        platform's written answer to them. `GET /api/disputes/{id}` rests on
        an unguessable id, and the per-task listing is open whenever
        TASK_AUTH_REQUIRED is off — which is the shipped default and how
        production runs — so on both routes an anonymous stranger reached both
        fields. This is the server declining to send them, rather than the
        console declining to draw them: the console's choice narrows nothing
        the API returns, and the API is what was being read.

        Withheld `reason` is the EMPTY STRING, not null, and not absent. The
        field is required and typed `str` on every client built against this
        API, so a null would not be read as "withheld" — it would fail the
        row's type check and cost the reader the whole dispute, statuses and
        refund hash included, which is more than is being withheld. Empty is
        unambiguous because `dispute_svc._require_reason` refuses a reason
        with nothing visible in it: no stored reason is ever empty, so an
        empty one on the wire can only mean this. `rejection_reason` is already nullable and is nulled, which is
        the same answer it gives for every dispute that was not rejected.

        A proof-carrying read is unchanged, as is every route that answers a
        caller who has already proved more than a token: the payer who just
        signed `POST /disputes`, and the adjudicator behind the operator key.
        The payer can also prove themselves on the two READ routes, with a
        dispute read grant (D-067) — see `TaskReadProof.proves_free_text`.

        `reason_withheld` says which answer this was, so a client never has to
        infer it from an empty string.
        """
        return cls(
            id=record.id,
            job_id_hex=record.job_id_hex,
            task_id=record.task_id,
            step_index=record.step_index,
            agent_id=record.agent_id,
            payer=record.payer,
            reason=record.reason if free_text else "",
            status=record.status,
            charged_usdc=record.charged_usdc,
            creditable_usdc=record.creditable_usdc,
            opened_at=record.opened_at,
            resolved_at=record.resolved_at,
            refund_tx=record.refund_tx,
            rating_tx=record.rating_tx,
            credited_usdc=record.credited_usdc,
            updated_at=record.updated_at,
            rating_confirmed=record.rating_confirmed,
            rejection_reason=record.note if free_text and record.status == "rejected" else None,
            reason_withheld=not free_text,
        )


class DuplicateDisputeResponse(BaseModel):
    """The `duplicate_dispute` 409 body: the error envelope, plus the dispute.

    One dispute per `(job_id, step)` is a product rule, and the second attempt
    is answered with the FIRST dispute unchanged — that is the acceptance
    criterion, and a bare error code cannot satisfy it. Declared as a model so
    the schema says so and the frontend can read `dispute` off the 409 instead
    of issuing a second request to find out what it already has.
    """

    detail: str
    error: dict[str, str]
    dispute: DisputeResponse


class CreditPolicy(BaseModel):
    """The terms an upheld dispute is paid under, as the buyer is shown them.

    Exists so the receipt can state the policy BEFORE the buyer signs anything,
    from the same setting the payout reads: ADR 0002 promises buyer and
    operator the terms in advance, and a dispute button that only reveals what
    it pays once it has been pressed does not keep that promise.

    `funded_by` and `adjudicated_by` are the trust model `refund_svc` discloses
    — the platform's own wallet pays the credit, and the platform decides the
    claim, with no on-chain arbitration behind it. Single-value literals, so
    the schema itself says there is no other answer today: the day there is
    one, widening the literal is a deliberate contract change rather than a
    string that quietly started meaning something else.
    """

    credited_fraction: float
    funded_by: Literal["platform"]
    adjudicated_by: Literal["platform"]

    @classmethod
    def in_force(cls, fraction: float) -> CreditPolicy:
        """The policy under `fraction`, clamped by the refund's own rule.

        `credited_amount_usdc` clamps the fraction inline, so the fraction it
        really applies is read back as what it credits on one whole USDC rather
        than re-clamped here. The clamp keeps one home, and a misconfigured 1.5
        is shown as the 1.0 that would actually be paid instead of a promise
        the payout would never keep.
        """
        return cls(
            credited_fraction=refund_svc.credited_amount_usdc(1.0, fraction),
            funded_by="platform",
            adjudicated_by="platform",
        )


class SettlementStepView(BaseModel):
    """One settled step, and what disputing it would credit.

    Exists because the trace a buyer reads is evicted from memory long before
    their window closes, and this is read off the settlement record instead —
    so it is the one account that survives of which agent ran each step, what
    it was charged, whether it delivered and what it produced. A mirror of
    `dispute_store.SettlementStep` rather than the dataclass itself, for
    `DisputeResponse`'s reason.

    `creditable_usdc` is computed HERE, with the refund's own rule, so the
    receipt never re-derives the rounding: it is the figure `open_dispute`
    would freeze onto a dispute opened now. What an uphold transfers is also
    bounded by the settled total (`refund_svc.creditable_for`), so this is the
    ceiling the buyer is shown, never a sum the platform could exceed.

    `output_summary` is untrusted — an external agent's own words — and is the
    line the world-readable trace already showed for the step, cleaned to
    `OUTPUT_SUMMARY_MAX_CHARS` by its writer before it was stored. It is passed
    through verbatim and escaped on render like every other stored string.
    None for a step that delivered nothing, and for every settlement recorded
    before 4.05.
    """

    step_index: int
    agent_id: str
    agent_name: str | None
    price_usdc: float
    delivered: bool
    creditable_usdc: float
    output_summary: str | None
    # What escrow v2 paid this step's operator, and the on-chain receipt that
    # payout minted — public already, in the `charged` event. Null on a v1
    # settlement, which paid one total for the run (ADR 0010).
    paid_usdc: float | None = None
    receipt_id_hex: str | None = None
    # Why a delivered v2 step was paid nothing ("free", "no_onchain_owner",
    # "owner_unreadable", "over_authorized_cap"); null otherwise.
    unpaid_reason: str | None = None

    @classmethod
    def of(cls, step: SettlementStep, fraction: float) -> SettlementStepView:
        """Project a settled step onto the wire, pricing its credit under `fraction`."""
        return cls(
            step_index=step.step_index,
            agent_id=step.agent_id,
            agent_name=step.agent_name,
            price_usdc=step.price_usdc,
            delivered=step.delivered,
            # Exactly 0.0 for a step that did not deliver, never its price times
            # the fraction: it was not billed, `open_dispute` refuses it, and a
            # receipt that priced a credit for it would promise money nobody
            # can claim.
            creditable_usdc=refund_svc.credited_amount_usdc(step.price_usdc, fraction) if step.delivered else 0.0,
            output_summary=step.output_summary,
            paid_usdc=step.paid_usdc,
            receipt_id_hex=step.receipt_id_hex,
            unpaid_reason=step.unpaid_reason,
        )


class SettlementView(BaseModel):
    """What a task settled as: the facts a buyer's FIRST dispute starts from.

    Exists because the per-task read used to carry only the deadline, and a
    dispute cannot be started from a deadline. The challenge is minted against
    the job id, only the payer's wallet may sign it, and the buyer has to see
    which step they are disputing and what it would credit — every one of which
    lived on the settlement record and nowhere a client could read it.

    A mirror of `dispute_store.SettlementRecord`, for `DisputeResponse`'s
    reason, and a deliberately narrower one: `auth_id_hex` is left out because
    nothing a client does needs it, and a field is only ever added to this
    shape for a reader who does.
    """

    # Both public already; this read saves a chain lookup and reveals nothing
    # else. The job id is an argument of `PaymentEscrow.charge` and a field of
    # the `charged` event it emits (receipt id, auth id, amount, job id). The
    # payer is NOT in that event: it is in the `authd` event the payer's own
    # `authorize` emitted (auth id, payer, max amount) and in the escrow's
    # public `authorization(auth_id)` view, joined to the charge by the auth id
    # both carry. And `GET /api/tasks/{task_id}` already serves the full
    # `charge_tx`, so a holder of a task id could walk task, charge tx, job id,
    # auth id, payer on-chain today. Neither value is a credential either:
    # minting a challenge against a job id is public by design, and opening a
    # dispute takes the payer's signature, which knowing the address does not
    # provide.
    job_id_hex: str
    payer: str
    settled_at: float
    window_closes_at: float
    settled_usdc: float
    charge_tx: str | None
    proof_tx: str | None
    steps: list[SettlementStepView]
    policy: CreditPolicy

    @classmethod
    def of(cls, record: SettlementRecord) -> SettlementView:
        """Project a settlement onto the wire, with its credits priced.

        The fraction is read from settings ONCE, so every step's credit and the
        stated policy come from one reading and cannot disagree within a
        response.
        """
        fraction = settings.dispute_credited_fraction
        return cls(
            job_id_hex=record.job_id_hex,
            payer=record.payer,
            settled_at=record.settled_at,
            window_closes_at=record.window_closes_at,
            settled_usdc=record.settled_usdc,
            charge_tx=record.charge_tx,
            proof_tx=record.proof_tx,
            steps=[SettlementStepView.of(s, fraction) for s in record.steps],
            policy=CreditPolicy.in_force(fraction),
        )


class TaskDisputesResponse(BaseModel):
    """A task's settlement, its dispute window, and everything raised against it.

    `settlement` and `window_closes_at` are null until the task settles — a
    task that was never paid for has nothing to dispute and no deadline to
    show. The deadline is read from the settlement record rather than
    recomputed from `DISPUTE_WINDOW_SECONDS`, so retuning that setting cannot
    move a deadline a buyer was already given. It stays at the top level,
    always equal to `settlement.window_closes_at`, because clients written
    before 4.05 read it there.

    `now` is this server's clock when the response was built. The window is a
    deadline the SERVER enforces, so a countdown run off the browser's clock is
    wrong by however far that clock has drifted: it shows a window open that
    `open_dispute` will refuse as closed, or closed while there is still time.
    The console measures the skew from this and corrects by it.
    """

    task_id: str
    window_closes_at: float | None
    now: float
    settlement: SettlementView | None
    disputes: list[DisputeResponse]
    # What happened to the run's money, as the task records it (ADR 0010):
    # "settled" whenever a settlement is on record, otherwise the task's own
    # `settlement` while this process still holds the task — so a run whose
    # settlement failed or is unconfirmed says so here rather than reading as
    # merely "not settled yet". Null when neither is known.
    settlement_state: SettlementState | None = None


def _refuse(exc: dispute_svc.DisputeError) -> HTTPException:
    """The rules lane's refusal, as this API's error — code only.

    The code is passed through as the HTTPException *detail* because
    `app/main.py`'s handler promotes a snake_case detail to `error.code`
    verbatim — so a code the service adds later reaches the frontend without a
    mapping table here to forget to update. The status comes from the exception
    for the same reason: this module must not hold a second opinion about
    whether a closed window is a 409.

    The MESSAGE is dropped here, and `_refuse_buyer` is where it is not. The
    adjudication refusals this builds quote things the buyer's side of the
    table should not read back — what a refund was capped at
    (`refund_svc.RefundRefused`'s message names `MAX_REFUND_USDC`), what state
    a credit is stuck in, which settlement is missing — and those messages are
    written for an operator holding the ledger. The stable code is what a
    client branches on, and it is unchanged.
    """
    return HTTPException(exc.status_code, exc.code)


def _refuse_buyer(exc: dispute_svc.DisputeError) -> HTTPException:
    """A refusal on one of the BUYER's routes, message and all.

    Everything `dispute_svc` refuses a buyer with is written for the buyer,
    and two of them say something the code alone cannot: a closed window names
    the time it closed, and a lapsed challenge says to ask for a new one.
    Dropping those made liars of the docstrings that promise them — and left
    the frontend to re-derive an English sentence the service had already
    written, which is how two spellings of one refusal start.

    Safe to disclose, which is the only reason this exists as a second
    function rather than as a change to `_refuse`. Every message reachable
    from `issue_dispute_challenge` and `open_dispute` either states a rule
    ("only the payer of a workflow may dispute it"), states a format ("an
    ed25519 signature is 64 bytes"), echoes back a value the caller sent —
    a step index the edge has already bounded to an int — or states a fact
    `SettlementView` already serves to anyone who can name the task: when the
    window closed, whether a step delivered, whether it was free. The mint
    refuses on those last three too. Anything that names PRIVATE state sits
    AFTER `_authenticate_payer` in the service's own order, so nobody reaches
    it without having signed as the payer first.

    That ordering is the whole of the argument, so it is the thing to re-read
    before routing a new code through here: a refusal moved to before the
    signature check is a refusal whose message this function would publish.
    """
    return CodedHTTPException(exc.status_code, exc.code, exc.message)


@router.post(
    "/disputes/challenge",
    response_model=DisputeChallengeResponse,
    summary="Mint a dispute challenge to sign",
)
async def dispute_challenge(body: DisputeChallengeReq, request: Request) -> DisputeChallengeResponse:
    """Issue the nonce and the exact string the payer's wallet must sign.

    The handler itself decides nothing: it validates the two fields, calls the
    service once, and renders the message the service defines. In particular it
    does NOT look up the settlement to decide whether a mint is allowed — the
    service does that, for `bind_challenge`'s reason (a bounded challenge table
    keyed on caller-supplied values may only ever hold pairs that could really
    be disputed, or anyone can evict the challenges honest buyers are mid-way
    through signing). A second opinion here would be a rule with two homes.

    So an unknown or unsettled job is refused at the mint, with the service's
    own code and status rather than a 500 — which is what `_refuse` is for, and
    why this cheap public route still has a `try`.

    The expiry it answers with is deliberately COARSE (`_coarse_expiry`): the
    mint is idempotent inside the window, so an exact one would tell any
    anonymous caller how long ago somebody started disputing a step they can
    name. The idempotency stays — it is what keeps a flood from cancelling the
    nonce a buyer is mid-way through signing — and only the clock is blurred.

    One client may mint `DISPUTE_CHALLENGE_RATE_LIMIT_PER_MINUTE` a minute
    (`_challenge_limiter`); past it, 429 `dispute_challenge_rate_limited` with
    Retry-After, before the settlement is read.
    """
    retry_after = _challenge_limiter.hit(client_identity(dict(request.scope)))
    if retry_after is not None:
        raise HTTPException(429, "dispute_challenge_rate_limited", headers={"Retry-After": str(retry_after)})
    try:
        nonce, expires_at = await dispute_svc.issue_dispute_challenge(body.job_id_hex, body.step_index)
    except dispute_svc.DisputeError as e:
        raise _refuse_buyer(e) from None
    # The nonce is a live single-use credential for its whole window: returned
    # to the caller, never written to a log — `bind_challenge`'s rule.
    logger.info("dispute challenge issued: job_id=%s step=%d", body.job_id_hex, body.step_index)
    return DisputeChallengeResponse(
        message=dispute_svc.dispute_message(body.job_id_hex, body.step_index, nonce),
        nonce=nonce,
        expires_at=_coarse_expiry(expires_at),
    )


_READ_CHALLENGE_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {
        "model": ErrorEnvelope,
        "description": (
            "`unknown_task`, or `no_settlement` — the task never settled, so there is no payer to prove; "
            "or `no_disputes` — it settled, but nothing was disputed, so there is nothing to read. With "
            "TASK_AUTH_REQUIRED on, a task that can still be disputed is minted for without a dispute, so "
            "its payer can reach the listing to raise a first one after a restart."
        ),
    },
    503: {
        "model": ErrorEnvelope,
        "description": "`challenge_capacity_dispute_read` — every read-challenge slot is live. Ask again shortly.",
    },
}

_READ_GRANT_RESPONSES: dict[int | str, dict[str, object]] = {
    403: {
        "model": ErrorEnvelope,
        "description": "`not_the_payer` — the signature is not the settlement's payer's over this read challenge.",
    },
    404: {"model": ErrorEnvelope, "description": "`unknown_task` or `no_settlement`."},
    409: {
        "model": ErrorEnvelope,
        "description": (
            "`challenge_unknown` — not this task's read challenge, or already used; "
            "`challenge_expired` — its five minutes are up. Either way, ask for a new one."
        ),
    },
}


@router.post(
    "/disputes/read-challenge",
    response_model=DisputeReadChallengeResponse,
    summary="Mint a challenge the payer signs to read their disputes' free text",
    responses=_READ_CHALLENGE_RESPONSES,
)
async def dispute_read_challenge(body: DisputeReadChallengeReq) -> DisputeReadChallengeResponse:
    """The first half of D-067's fix: a nonce for the payer to sign.

    Public, like `/disputes/challenge`, and safe for the same reasons: it mints
    only for a task that has a settlement AND at least one dispute — or, under
    TASK_AUTH_REQUIRED, one that could still be disputed, so a payer whose
    token a restart forgot can reach the listing for a first dispute — into the
    `dispute_read` budget, and a live challenge comes back as is, so a flood
    cannot cancel the one the payer is signing. The service decides; this
    renders.
    """
    try:
        nonce, expires_at = await dispute_read.issue_read_challenge(body.task_id)
    except dispute_svc.DisputeError as e:
        raise _refuse_buyer(e) from None
    logger.info("dispute read challenge issued: task_id=%s", body.task_id)
    return DisputeReadChallengeResponse(
        nonce=nonce,
        message=dispute_read_message(body.task_id, nonce),
        expires_at=_coarse_expiry(expires_at),
    )


@router.post(
    "/disputes/read-grant",
    response_model=DisputeReadGrantResponse,
    summary="Exchange the payer's read signature for a read grant",
    responses=_READ_GRANT_RESPONSES,
)
async def dispute_read_grant(body: DisputeReadGrantReq) -> DisputeReadGrantResponse:
    """The second half: verify the payer's signature and hand back a grant.

    The grant goes back as `X-Dispute-Read-Grant` on `GET /tasks/{id}/disputes`
    and `GET /disputes/{id}`, and buys exactly the two free-text fields there.
    It is not a task token and nothing treats it as one: no artifact, no trace,
    no stream. None of the opening rules are asked — the window may be long
    closed — because reading what was said is not making a new claim.

    The grant is never logged: it is a credential for its hour.
    """
    try:
        grant, expires_at = await dispute_read.grant_read(body.task_id, body.nonce, body.signature_b64)
    except dispute_svc.DisputeError as e:
        logger.warning("dispute read grant refused: task_id=%s reason=%s", body.task_id, e.code)
        raise _refuse_buyer(e) from None
    logger.info("dispute read grant issued: task_id=%s", body.task_id)
    return DisputeReadGrantResponse(grant=grant, expires_at=expires_at)


def _duplicate_envelope(exc: dispute_svc.DisputeError, existing: DisputeRecord) -> JSONResponse:
    """A 409 that still carries the dispute the caller already has.

    `app/main.py`'s exception handler builds every error body, and it has no
    room for a payload — an HTTPException carries a detail, not a record. So
    this one response is assembled here, in the handler's exact shape: the same
    `detail`, the same `error` object, the same request id, plus `dispute`.
    Anything a client reads off a normal error it can still read off this one.

    The duplication is deliberate and bounded to this function rather than
    imported from `main`, which imports this module — the cycle is the reason,
    and `tests/test_dispute_api.py` pins the two shapes together so they cannot
    drift apart unnoticed.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=DuplicateDisputeResponse(
            detail=exc.code,
            error={"code": exc.code, "message": exc.message, "request_id": request_id_var.get()},
            # With its free text: only a caller whose wallet signature already
            # verified as this settlement's payer gets this far, and the text
            # is their own. They are the one party it was never withheld from.
            dispute=DisputeResponse.of(existing, free_text=True),
        ).model_dump(),
    )


@router.post(
    "/disputes",
    response_model=DisputeResponse,
    summary="Open a dispute on a settled step",
    responses={
        409: {
            "model": DuplicateDisputeResponse,
            "description": "This step is already disputed — the body carries the original dispute unchanged.",
        }
    },
)
async def open_dispute(body: OpenDisputeReq) -> DisputeResponse | JSONResponse:
    """Verify the payer's signature and record the dispute.

    Every check — the challenge is live, the signature is the payer's, the
    payer is the wallet that actually paid this job, the step settled and was
    charged, the window is still open — belongs to `dispute_svc.open_dispute`
    and is made there in one place, against one settlement record. Splitting
    any of it out to here would mean a rule with two homes and one of them
    unguarded: 4.03's resolution path calls the service, not this route.

    200, never 201, and never a second record: a repeat of a dispute already
    raised is answered with the first one (see `_duplicate_envelope`), so a
    buyer who double-submits or refreshes sees what they filed rather than an
    error they cannot act on.
    """
    try:
        record = await dispute_svc.open_dispute(
            job_id_hex=body.job_id_hex,
            step_index=body.step_index,
            reason=body.reason,
            payer=body.payer,
            nonce=body.nonce,
            signature_b64=body.signature_b64,
        )
    except dispute_svc.DisputeError as e:
        # The refusal is logged with the job, the step and the code — never the
        # claimed payer or the buyer's reason text. The payer is attacker-chosen
        # until the signature verifies (`bind`'s rule for the claimed signer),
        # and the reason is the buyer's own words, which do not belong in an
        # operator's log viewer.
        logger.warning("dispute refused: job_id=%s step=%d reason=%s", body.job_id_hex, body.step_index, e.code)
        if e.existing is not None:
            return _duplicate_envelope(e, e.existing)
        raise _refuse_buyer(e) from None
    logger.info(
        "dispute opened: id=%s task_id=%s job_id=%s step=%d agent_id=%s charged_usdc=%s",
        record.id,
        record.task_id,
        record.job_id_hex,
        record.step_index,
        record.agent_id,
        record.charged_usdc,
    )
    # The payer just proved themselves with a signature over this job and
    # step, so they read back what they wrote.
    return DisputeResponse.of(record, free_text=True)


@router.get("/disputes/{dispute_id}", response_model=DisputeResponse, summary="Read one dispute")
async def get_dispute(
    dispute_id: Annotated[str, Path(min_length=1, max_length=64)],
    proof: Annotated[TaskReadProof, Depends(task_read_proof)],
) -> DisputeResponse:
    """The dispute, by the id `POST /disputes` returned.

    Unauthenticated by design: `dispute_store.new_dispute_id` mints an
    unguessable id, so the id IS the capability — the same trade the per-task
    read token makes, without a token to lose. A buyer with no account can
    still come back to their dispute from a link.

    That trade only ever held while the ids stayed unguessable, and they do
    not: `GET /api/tasks/{id}/disputes` PUBLISHES them, and it is open while
    TASK_AUTH_REQUIRED is off, which is how production runs. So the id buys
    the money facts — status, amounts, the refund hash — and the free text is
    bought separately, with the same proof that route asks for: a task token
    for the task this dispute belongs to, the operator key, or a dispute read
    grant the dispute's own payer earned by signature (D-067). The dispute
    names its own task, so the caller supplies a credential and never a
    second id.

    The id's exact format is deliberately not pinned in the path pattern. The
    store mints it and 4.03 may lengthen it; a pattern here would make that a
    two-module change, and would answer a mistyped id with a 422 that says
    "wrong shape" where 404 says all a caller is entitled to know.
    """
    record = await dispute_svc.get_dispute(dispute_id)
    if record is None:
        raise HTTPException(404, "unknown_dispute")
    # The path names no task, so nothing has read this dispute's task back from
    # the durable store yet: after a restart its token could not prove until
    # something else had (D-090). Held here first, best effort, exactly as
    # `task_read_proof` holds a `{task_id}` route's — a store that cannot answer
    # leaves the token unproven and the free text withheld, never a failed read.
    try:
        await task_persistence.ensure_task(record.task_id)
    except task_persistence.TaskStoreUnavailable:
        pass
    # Against the payer the DISPUTE names, never one the caller supplies: a read
    # grant is honoured only for the party whose signature earned it.
    return DisputeResponse.of(record, free_text=proof.proves_free_text(record.task_id, record.payer))


async def _require_listing_read(
    task_id: str,
    proof: Annotated[TaskReadProof, Depends(task_read_proof)],
) -> None:
    """`GET /tasks/{id}/disputes`'s gate while TASK_AUTH_REQUIRED is on (D-067).

    Admits exactly what buys the listing's free text,
    `TaskReadProof.proves_free_text`: a task token or the operator key — what
    `require_task_read` admits — or a live dispute read grant for THIS task and
    ITS settlement's payer. `require_task_read` took the first two only, so a
    payer whose token was gone could not reach the one route their receipt
    reads, grant in hand.

    A dependency rather than a check in the handler, for `require_task_read`'s
    reason: it runs before the route's own path bound, so an over-long id is
    this 404 rather than a 422 that quotes the id back. The settlement is read
    only for a caller who sent a grant and was not admitted on a token or the
    key, and never for an id past the path's bound. Every refusal is the same
    bare `unknown_task` 404, so a refused caller cannot tell a task that exists
    from one that does not.
    """
    if not settings.task_auth_required or proof.proves(task_id):
        return
    if proof.read_grant and len(task_id) <= _MAX_TASK_ID_CHARS:
        settlement = await dispute_svc.settlement_for_task(task_id)
        if settlement is not None and proof.proves_free_text(task_id, settlement.payer):
            return
    raise HTTPException(404, "unknown_task")


@router.get(
    "/tasks/{task_id}/disputes",
    response_model=TaskDisputesResponse,
    summary="A task's dispute window and the disputes raised on it",
    dependencies=[Depends(_require_listing_read)],
)
async def list_task_disputes(
    task_id: Annotated[str, Path(min_length=1, max_length=_MAX_TASK_ID_CHARS)],
    proof: Annotated[TaskReadProof, Depends(task_read_proof)],
) -> TaskDisputesResponse:
    """The settlement, the window and what has been raised, in one read.

    Lives here rather than in `routers/tasks.py` so the dispute surface is one
    module: `tasks.py` owns the in-memory task state and knows nothing about
    settlements, and a route split across the two would have to be found twice.

    An unsettled or unknown task is **not** a 404 — it is a null settlement, a
    null window and an empty list, with `now` still set. The console polls this
    while a workflow runs, and the honest answer to "can this be disputed yet?"
    before settlement is "no, and here is nothing", not an error the UI has to
    special-case into the same view.

    THE TASK ID IS NOT A CAPABILITY, and this route is built on the assumption
    that it is not. While TASK_AUTH_REQUIRED is off — the shipped default, and
    how production runs — it is open like every other `/tasks/{task_id}/...`
    read, and `GET /api/tasks?limit=200` hands out two hundred task ids for the
    asking. So in production this read is open, and what it may carry is
    decided on that basis rather than on the gate.

    With TASK_AUTH_REQUIRED ON it is gated, but NOT by `require_task_read`
    (D-067). That dependency admits a task token or the operator key and
    nothing else, and it ran before the handler — so a payer holding a dispute
    read grant, whose token died with the tab or the process, was answered 404
    `unknown_task` before the grant was ever looked at, from the one route that
    feeds their receipt. It is gated here instead, on exactly what buys the
    free text: `proof.proves_free_text` — the token or the key, which
    `require_task_read` would take, or a live grant for this task and its
    settlement's payer. Nothing wider: a grant for another task, another payer
    or no settlement at all still reads as `unknown_task`. See
    `_require_listing_read`.

    What it carries openly is held to what is already public. The settlement's
    job id and payer are on-chain (see `SettlementView`), each output summary
    is the line the world-readable trace already showed, and every dispute's
    amounts, statuses, timestamps and transaction hashes are facts about money
    that moved or is owed. Nothing belongs on either shape that the chain or
    the trace does not already publish.

    The two fields that do not meet that standard are the HUMAN-WRITTEN ones —
    each dispute's `reason` and, on a rejection, `rejection_reason` — and they
    are withheld from a caller who has not proved they may read this task,
    with the proof `require_task_read` would demand if it were switched on — a
    task token, or the operator key — or with a dispute read grant, which the
    task's payer earns by signing and which buys this free text and nothing
    that `require_task_read` guards. See `DisputeResponse.of`. This route
    stays open to a shared trace link, which is the point of not simply
    turning the switch on; a shared link just does not come with the buyer's
    words attached.
    """
    settlement = await dispute_svc.settlement_for_task(task_id)
    # Resolved ONCE for the whole listing, not per dispute: every row here
    # belongs to this one task, so one proof answers for all of them, and a
    # per-row answer would invite a future row that disagreed with its
    # neighbours about who was reading.
    #
    # A read grant is checked against the SETTLEMENT's payer, the one the
    # grant route proved the signature against; with no settlement there is
    # no payer, and a grant buys nothing.
    free_text = proof.proves_free_text(task_id, settlement.payer if settlement is not None else None)
    disputes = await dispute_svc.list_for_task(task_id)
    return TaskDisputesResponse(
        task_id=task_id,
        window_closes_at=settlement.window_closes_at if settlement is not None else None,
        # Read after both lookups, so the clock the console corrects by is as
        # close to the moment the response leaves as this handler can get it.
        now=time.time(),
        settlement=SettlementView.of(settlement) if settlement is not None else None,
        disputes=[DisputeResponse.of(d, free_text=free_text) for d in disputes],
        settlement_state=_settlement_state(task_id, settlement),
    )


def _settlement_state(task_id: str, settlement: SettlementRecord | None) -> SettlementState | None:
    """The receipt's settlement state: the record's word first, then the task's."""
    if settlement is not None:
        return "settled"
    task = state.tasks.get(task_id)
    return task.settlement if task is not None else None


# Every status the adjudication pair actually answers with, so the published
# spec stops understating them. It advertises the production server, so a
# generated client reads it as the contract: with only 422/429/500 merged in by
# `include_router`, the 401 an operator meets on their first call, the 503 a
# deployment with the switch off answers its operator, and the 502/504 that say
# whether a credit moved were all absent — and a 504 that means "this may still
# land, reconcile by hand, never retry" is the last thing to leave undeclared.
#
# 403 is deliberately NOT here: `require_adjudicator` never gives one. A
# missing key and a wrong key are both 401, on purpose, because a route that
# distinguishes them is an oracle — see its docstring.
_ADJUDICATION_RESPONSES: dict[int | str, dict[str, object]] = {
    401: {
        "model": ErrorEnvelope,
        "description": "`invalid_api_key` — no X-API-Key, or not the operator's. The two are one answer.",
    },
    404: {"model": ErrorEnvelope, "description": "`unknown_dispute` — no dispute with that id."},
    409: {
        "model": ErrorEnvelope,
        "description": (
            "The dispute cannot take this verdict: `dispute_not_open`, `dispute_rejected`, "
            "`refund_in_flight`, `settlement_missing`, `nothing_to_credit` or `refund_above_cap`."
        ),
    },
    503: {
        "model": ErrorEnvelope,
        "description": (
            "`adjudication_not_configured` — no API_KEY is configured, so nobody can adjudicate; or, "
            "once the key has matched, `dispute_refunds_disabled` — the refund switch is off. "
            "Both are the operator's, never the caller's."
        ),
    },
}

# Uphold alone can reach the ledger, so only it can answer about one.
_UPHOLD_RESPONSES: dict[int | str, dict[str, object]] = {
    **_ADJUDICATION_RESPONSES,
    502: {
        "model": ErrorEnvelope,
        "description": (
            "`refund_failed` — the ledger rejected the credit, so NOTHING moved and the dispute stays upheld."
        ),
    },
    504: {
        "model": ErrorEnvelope,
        "description": (
            "`refund_unconfirmed` — the credit was submitted and its outcome is unknown. It may still "
            "land, so it must be reconciled by hand and NEVER retried."
        ),
    },
}


@router.post(
    "/disputes/{dispute_id}/uphold",
    response_model=DisputeResponse,
    summary="Uphold a dispute and credit the buyer",
    dependencies=[Depends(require_adjudicator)],
    responses=_UPHOLD_RESPONSES,
)
async def uphold_dispute(
    dispute_id: str = Path(..., min_length=1, max_length=64),
) -> DisputeResponse:
    """Find for the buyer: credit the step back out of the settler's balance.

    The guard is spelled on the decorator rather than hoisted onto a secured
    sub-router, `list_task_disputes`-style and for the same reason — with the
    dependency on the route, what admits a caller is readable at the route,
    and `tests/test_money_route_auth.py` pins both of this pair so a third
    adjudication route added without one fails there rather than in
    production.

    This handler decides nothing about the money, exactly as `open_dispute`
    decides nothing about the window. Whether the dispute may be upheld at
    all, how much is creditable once the step price and the settled total are
    taken into account, and — the part that must never be duplicated — whether
    a transfer has already been claimed for this dispute, all belong to
    `dispute_svc.uphold`. A second opinion here would be a second place that
    could decide to sign.

    So a REPEAT uphold is the service's answer, passed through unchanged: the
    same terminal record, carrying the same `refund_tx`. Not an error, and
    emphatically not a second payout — an adjudicator who double-clicks, or a
    console that retries a dropped response, must be able to see what already
    happened rather than be told something went wrong.
    """
    try:
        record = await dispute_svc.uphold(dispute_id)
    except dispute_svc.DisputeError as e:
        # The id and the code, which is all this layer holds: on a refusal
        # there is no record here to read a job, a payer or an amount off.
        # `dispute_svc` has the record and logs the money-path detail the
        # frozen contract requires; duplicating it here would mean guessing.
        logger.warning("uphold refused: dispute_id=%s reason=%s", dispute_id, e.code)
        raise _refuse(e) from None
    # The payer IS logged here, unlike in `open_dispute` where it is refused a
    # line: this one came off the stored record, so it is the address a wallet
    # signature already proved — not an attacker's claim. It is also the
    # address that was just paid, which is the whole point of the line.
    logger.info(
        "dispute upheld: id=%s job_id=%s task_id=%s step=%d payer=%s status=%s creditable_usdc=%s refund_tx=%s",
        record.id,
        record.job_id_hex,
        record.task_id,
        record.step_index,
        record.payer,
        record.status,
        record.creditable_usdc,
        record.refund_tx,
    )
    # `require_adjudicator` admitted this caller on the operator key, which is
    # strictly more than the free text is gated on.
    return DisputeResponse.of(record, free_text=True)


@router.post(
    "/disputes/{dispute_id}/reject",
    response_model=DisputeResponse,
    summary="Reject a dispute, with a reason the buyer is shown",
    dependencies=[Depends(require_adjudicator)],
    # No 502/504: nothing here signs, so there is no ledger for it to report
    # on. Its own 422 — `rejection_reason_required`, a note that cleaned to
    # nothing — shares the row `include_router` already merges in.
    responses=_ADJUDICATION_RESPONSES,
)
async def reject_dispute(
    body: RejectDisputeReq,
    dispute_id: str = Path(..., min_length=1, max_length=64),
) -> DisputeResponse:
    """Find for the platform: close the dispute without crediting anything.

    Behind `require_adjudicator` alongside `uphold_dispute`, although nothing
    here signs. Rejecting is the other half of one decision, and the half that
    is CHEAP to make is exactly the half an attacker would reach for: a
    rejection is terminal, so an open reject route would let anyone close
    every dispute raised against the platform before an adjudicator ever saw
    one. The refund switch gates it too, for the same reason — a deployment
    that cannot pay a dispute out must not be able to dispose of one either —
    but only once the key has matched, so the switch's state is the
    operator's to read and nobody else's (D-052).

    The note is REQUIRED and it is the buyer's to read — it comes back as the
    dispute's `rejection_reason`. The body is required with it, so no body, no
    note, a null note and an empty one are all the same field-level 422, and
    the service is never called. The note is then passed through rather than
    interpreted: whether what is left of it after cleaning still says anything
    is the service's call, because only the service cleans it, and its
    `rejection_reason_required` reaches the adjudicator through `_refuse`
    like every other code — 422, verbatim, with no mapping to add here.

    ONE ANSWER HERE PRECEDES THE GUARD, deliberately. FastAPI decodes the body
    before it solves dependencies, so a body that is not JSON at all — `{not
    json` — is a 422 rather than the 401 this pair otherwise gives an
    anonymous caller (503 `adjudication_not_configured` on a keyless one). It
    was worth checking what that discloses, and the answer is nothing:

      * the ROUTE'S EXISTENCE is already disclosed by the guarded answer. A
        well-formed anonymous POST here gets 401 `invalid_api_key`, where a
        path that does not exist gets 404. Whoever the 422 would tell has
        already been told;
      * the SHAPE is not disclosed at all. A well-formed body with no `note`
        is 401 too — the model is validated after the dependency like
        everything else, and only an undecodable body is answered earlier. So
        the 422 says "this endpoint parses JSON" and no more;
      * NOTHING RUNS. No store read, no signature, no money — the request dies
        in the body decode.

    Taking the body as a raw `Request` and parsing it by hand after the guard
    would close it, at the cost of the declared model that makes every promise
    in the paragraph above, and of the request schema in the published spec.
    That is a worse route in exchange for an answer nobody learns anything
    from. `tests/test_money_route_auth.py` pins the case so the next reader
    finds a decision here rather than an oversight.
    """
    try:
        record = await dispute_svc.reject(dispute_id, note=body.note)
    except dispute_svc.DisputeError as e:
        # `uphold_dispute`'s rule, and the note is left out for
        # `open_dispute`'s: free text written by a human about a specific
        # complaint does not belong in an operator's log viewer.
        logger.warning("reject refused: dispute_id=%s reason=%s", dispute_id, e.code)
        raise _refuse(e) from None
    # Nothing about the note, not even whether there was one: every rejection
    # that gets this far carries one, so a flag could only ever say yes.
    logger.info(
        "dispute rejected: id=%s job_id=%s task_id=%s step=%d payer=%s status=%s",
        record.id,
        record.job_id_hex,
        record.task_id,
        record.step_index,
        record.payer,
        record.status,
    )
    # `uphold_dispute`'s reason: the operator key, and the note is the
    # adjudicator's own, written moments ago in this request.
    return DisputeResponse.of(record, free_text=True)
