"""The dispute window's HTTP surface — story 4.02 (ADR 0002).

A buyer who paid for a step that did not deliver what it promised has 24 hours
from settlement to say so. Four routes are the whole of how they say it: mint a
challenge, sign it with the wallet that paid, post the dispute, and read back
what was raised on a task. Two more — added by story 4.03 — are how the
platform answers: uphold, and credit; or reject, and say so.

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
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..security import request_id_var, require_adjudicator
from ..services import dispute_svc, refund_svc
from ..services.dispute_store import DisputeRecord, DisputeStatus, SettlementRecord, SettlementStep
from ..task_auth import require_task_read

logger = logging.getLogger(__name__)

router = APIRouter(tags=["disputes"])

# A 16-byte job id, hex — the same shape `ChargeReq` and `SealReq` take, so a
# job id that could never have been charged is refused before any lookup.
_JOB_ID_PATTERN = r"^[0-9a-fA-F]{32}$"

# A Stellar G-address, as `SealReq.orchestrator` spells it. The payer is
# claimed, not trusted: the signature is what proves it, and this only bounds
# what reaches the verifier.
_PAYER_PATTERN = r"^G[A-Z2-7]{55}$"

# Twice the 32-entry cap `POST /api/stellar/server/seal` puts on a workflow's
# agents and receipts, so a plan that grows is not refused *here* first. The
# real check is that the settlement record actually has such a step, which only
# the service can make.
_MAX_STEP_INDEX = 63


class DisputeChallengeReq(BaseModel):
    """What the wallet is about to sign is derived from these two, so both are
    supplied when the challenge is minted rather than first seen at open time."""

    job_id_hex: str = Field(..., pattern=_JOB_ID_PATTERN)
    step_index: int = Field(..., ge=0, le=_MAX_STEP_INDEX)


class DisputeChallengeResponse(BaseModel):
    # The exact string the wallet must sign, returned rather than assembled
    # client-side so the format can version without shipping a new frontend —
    # `BindChallengeResponse`'s reasoning, and the same trade.
    message: str
    nonce: str
    expires_at: float


class OpenDisputeReq(BaseModel):
    job_id_hex: str = Field(..., pattern=_JOB_ID_PATTERN)
    step_index: int = Field(..., ge=0, le=_MAX_STEP_INDEX)
    # Bounded by the service's own ceiling, never a second number. The service
    # cleans every reason and trims it to MAX_REASON_CHARS, so an edge bound
    # above that accepted a paragraph and then stored only its start: a
    # 1,500-character reason was cut to 500 without a word to the buyer. At the
    # same constant it is a 422 they can see and fix. The one trim left is the
    # service's marker redaction lengthening a reason already at the bound,
    # which only text that forges a prompt-fence marker can reach.
    reason: str = Field(..., min_length=1, max_length=dispute_svc.MAX_REASON_CHARS)
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
    """The adjudicator's optional word on why a dispute was not upheld.

    Bounded identically to `OpenDisputeReq.reason` — one paragraph, no empty
    string — because it is the same kind of thing from the other side of the
    table, and a rejection note that outgrew the complaint it answers would be
    the one free-text field in this surface nobody had sized. Identically down
    to the number: `dispute_svc.reject` cleans and trims the note to the same
    MAX_REASON_CHARS, so a longer bound here would record an adjudicator's
    rationale cut short with nothing to say it was.

    Optional, and optional all the way down: the body itself may be absent, so
    a console that has nothing to add posts no body rather than an empty one.
    `min_length=1` then means "if you send a note, send a note" — a note of ""
    is refused rather than stored, so the absence of a reason has exactly one
    representation in the record instead of two.
    """

    note: str | None = Field(default=None, min_length=1, max_length=dispute_svc.MAX_REASON_CHARS)


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

    @classmethod
    def of(cls, record: DisputeRecord) -> DisputeResponse:
        """Project a stored record onto the wire shape."""
        return cls(
            id=record.id,
            job_id_hex=record.job_id_hex,
            task_id=record.task_id,
            step_index=record.step_index,
            agent_id=record.agent_id,
            payer=record.payer,
            reason=record.reason,
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


def _refuse(exc: dispute_svc.DisputeError) -> HTTPException:
    """The rules lane's refusal, as this API's error.

    The code is passed through as the HTTPException *detail* because
    `app/main.py`'s handler promotes a snake_case detail to `error.code`
    verbatim — so a code the service adds later reaches the frontend without a
    mapping table here to forget to update. The status comes from the exception
    for the same reason: this module must not hold a second opinion about
    whether a closed window is a 409.
    """
    return HTTPException(exc.status_code, exc.code)


@router.post(
    "/disputes/challenge",
    response_model=DisputeChallengeResponse,
    summary="Mint a dispute challenge to sign",
)
async def dispute_challenge(body: DisputeChallengeReq) -> DisputeChallengeResponse:
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
    """
    try:
        nonce, expires_at = await dispute_svc.issue_dispute_challenge(body.job_id_hex, body.step_index)
    except dispute_svc.DisputeError as e:
        raise _refuse(e) from None
    # The nonce is a live single-use credential for its whole window: returned
    # to the caller, never written to a log — `bind_challenge`'s rule.
    logger.info("dispute challenge issued: job_id=%s step=%d", body.job_id_hex, body.step_index)
    return DisputeChallengeResponse(
        message=dispute_svc.dispute_message(body.job_id_hex, body.step_index, nonce),
        nonce=nonce,
        expires_at=expires_at,
    )


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
            dispute=DisputeResponse.of(existing),
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
        raise _refuse(e) from None
    logger.info(
        "dispute opened: id=%s task_id=%s job_id=%s step=%d agent_id=%s charged_usdc=%s",
        record.id,
        record.task_id,
        record.job_id_hex,
        record.step_index,
        record.agent_id,
        record.charged_usdc,
    )
    return DisputeResponse.of(record)


@router.get("/disputes/{dispute_id}", response_model=DisputeResponse, summary="Read one dispute")
async def get_dispute(
    dispute_id: str = Path(..., min_length=1, max_length=64),
) -> DisputeResponse:
    """The dispute, by the id `POST /disputes` returned.

    Unauthenticated by design: `dispute_store.new_dispute_id` mints an
    unguessable id, so the id IS the capability — the same trade the per-task
    read token makes, without a token to lose. A buyer with no account can
    still come back to their dispute from a link.

    The id's exact format is deliberately not pinned in the path pattern. The
    store mints it and 4.03 may lengthen it; a pattern here would make that a
    two-module change, and would answer a mistyped id with a 422 that says
    "wrong shape" where 404 says all a caller is entitled to know.
    """
    record = await dispute_svc.get_dispute(dispute_id)
    if record is None:
        raise HTTPException(404, "unknown_dispute")
    return DisputeResponse.of(record)


@router.get(
    "/tasks/{task_id}/disputes",
    response_model=TaskDisputesResponse,
    summary="A task's dispute window and the disputes raised on it",
    dependencies=[Depends(require_task_read)],
)
async def list_task_disputes(
    task_id: str = Path(..., min_length=1, max_length=128),
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

    Gated by `require_task_read` like every other `/tasks/{task_id}/...` read:
    a dispute names its payer and carries the buyer's own words about the work,
    which is exactly the material that capability token exists to scope. But
    the dependency is a no-op while TASK_AUTH_REQUIRED is off — the shipped
    default, and how production runs — so there this read is world-readable,
    and it fails closed only once enforcement is turned on. That is why the
    settlement it carries is held to what is already public: the job id and the
    payer are on-chain (see `SettlementView`), and each output summary is the
    line the world-readable trace already showed. Nothing belongs on that shape
    that the chain or the trace does not already publish.
    """
    settlement = await dispute_svc.settlement_for_task(task_id)
    disputes = await dispute_svc.list_for_task(task_id)
    return TaskDisputesResponse(
        task_id=task_id,
        window_closes_at=settlement.window_closes_at if settlement is not None else None,
        # Read after both lookups, so the clock the console corrects by is as
        # close to the moment the response leaves as this handler can get it.
        now=time.time(),
        settlement=SettlementView.of(settlement) if settlement is not None else None,
        disputes=[DisputeResponse.of(d) for d in disputes],
    )


@router.post(
    "/disputes/{dispute_id}/uphold",
    response_model=DisputeResponse,
    summary="Uphold a dispute and credit the buyer",
    dependencies=[Depends(require_adjudicator)],
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
    return DisputeResponse.of(record)


@router.post(
    "/disputes/{dispute_id}/reject",
    response_model=DisputeResponse,
    summary="Reject a dispute, optionally with a note",
    dependencies=[Depends(require_adjudicator)],
)
async def reject_dispute(
    dispute_id: str = Path(..., min_length=1, max_length=64),
    body: RejectDisputeReq | None = None,
) -> DisputeResponse:
    """Find for the platform: close the dispute without crediting anything.

    Behind `require_adjudicator` alongside `uphold_dispute`, although nothing
    here signs. Rejecting is the other half of one decision, and the half that
    is CHEAP to make is exactly the half an attacker would reach for: a
    rejection is terminal, so an open reject route would let anyone close
    every dispute raised against the platform before an adjudicator ever saw
    one. The refund switch gates it too, for the same reason — a deployment
    that cannot pay a dispute out must not be able to dispose of one either.

    The note is the adjudicator's, and it is passed through rather than
    interpreted: this handler does not decide that a rejection needs a reason,
    because whether one is required is a policy the service owns and would
    otherwise hold an opinion about in two places.
    """
    note = body.note if body is not None else None
    try:
        record = await dispute_svc.reject(dispute_id, note=note)
    except dispute_svc.DisputeError as e:
        # `uphold_dispute`'s rule, and the note is left out for
        # `open_dispute`'s: free text written by a human about a specific
        # complaint does not belong in an operator's log viewer.
        logger.warning("reject refused: dispute_id=%s reason=%s", dispute_id, e.code)
        raise _refuse(e) from None
    logger.info(
        "dispute rejected: id=%s job_id=%s task_id=%s step=%d payer=%s status=%s noted=%s",
        record.id,
        record.job_id_hex,
        record.task_id,
        record.step_index,
        record.payer,
        record.status,
        note is not None,
    )
    return DisputeResponse.of(record)
