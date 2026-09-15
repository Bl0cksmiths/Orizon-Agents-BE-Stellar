"""
/api/stellar/* — surfaces the deployed Soroban contracts to the frontend.

Read routes simulate RPC calls (no signing).
Write routes have two shapes:
  - build-*   → returns unsigned XDR for Freighter to sign
  - submit    → takes Freighter-signed XDR and broadcasts it
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from ..config import settings
from ..schemas import AGENT_ID_PATTERN
from ..security import require_api_key
from ..services import registry_sync, reputation_svc
from ..services.dispatch_signing import dispatch_signer_address
from ..state import state
from ..stellar import cache as rcache
from ..stellar import client as sc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stellar", tags=["stellar"])


# ── response models (shapes match the existing payloads exactly) ──
class NetworkInfo(BaseModel):
    network: str
    rpc_url: str
    network_passphrase: str
    admin: str
    # The key that signs OUTBOUND dispatch envelopes, so an operator can verify
    # a request really came from Orizon. Deliberately its own field and NOT an
    # alias of `admin`: those are independent settings, and USING_CONTRACTS.md
    # plans to split the on-chain roles onto separate keys — an operator who
    # pinned `admin` would then see every signature turn into a forgery.
    # None when unconfigured; dispatch is unsigned rather than failing.
    dispatch_signer: str | None = None
    asset: str
    asset_sac: str
    contracts: dict[str, str]


class AgentRead(BaseModel):
    agent: Any


class ReputationInfo(BaseModel):
    """Smoothed reputation for one agent (mirror of reputation_svc.RepInfo)."""

    agent_id: str
    smoothed_bps: int  # prior-smoothed mean, 0..10_000
    lower_bound_bps: int  # conservative bound used for the routing floor
    avg_bps: int  # unsmoothed decayed on-chain mean (0 = no evidence)
    count: int  # lifetime rating count
    weight: int  # decayed evidence mass, stroops
    disputed: int  # lifetime dispute count
    dispute_rate_bps: int  # disputed / count, in bps
    source: Literal["onchain", "prior"]


class ReputationBatch(BaseModel):
    """Reputation for every registered agent + the routing constants."""

    reputations: dict[str, ReputationInfo]
    floor_bps: int
    prior_bps: int


class ReputationParams(BaseModel):
    """Full parameter set of the reputation system — routing constants applied
    by the backend plus the deployed ledger's on-chain decay constants."""

    enabled: bool
    prior_bps: int  # Bayesian prior mean, bps of the 0-100 scale
    prior_weight_usdc: float  # evidence mass of the prior, USDC
    floor_bps: int  # routing floor on the Wilson lower bound
    max_rating_weight_usdc: float  # per-rating weight cap
    read_ttl_seconds: float  # cache TTL for on-chain rep_state reads
    wilson_z: float  # z of the one-sided lower confidence bound
    epoch_seconds: int  # on-chain decay epoch length
    decay_bps_per_epoch: int  # evidence retained per epoch, bps
    max_decay_epochs: int  # full-forget horizon
    contract_id: str  # deployed ReputationLedger id ("" if unset)
    network: str


class AttestationRead(BaseModel):
    attestation: Any


class XdrResponse(BaseModel):
    xdr: str


class AuthorizeXdrResponse(BaseModel):
    xdr: str
    expires_at: int


class NewIdResponse(BaseModel):
    id_hex: str


# FE polls the read routes; identical simulate calls within this window are
# served from a tiny in-process cache instead of re-hitting Soroban RPC.
READ_TTL_SECONDS = 3.0


# ── meta ────────────────────────────────────────────────────────
@router.get("/network", response_model=NetworkInfo)
async def network() -> NetworkInfo:
    ids = sc.contract_ids()
    return NetworkInfo(
        network=settings.stellar_network,
        rpc_url=settings.stellar_rpc_url,
        network_passphrase=sc.network_passphrase(),
        admin=settings.stellar_admin_address,
        dispatch_signer=dispatch_signer_address(),
        asset="native",
        asset_sac=ids.asset_sac,
        contracts={
            "agent_registry": ids.agent_registry,
            "reputation_ledger": ids.reputation_ledger,
            "payment_escrow": ids.payment_escrow,
            "attestation_registry": ids.attestation_registry,
        },
    )


# AGENT_ID_PATTERN now lives in app/schemas.py — the binding router bounds an
# agent id with the same rule, and an SSRF-adjacent charset must have one owner.

# Job/auth ids are 16-byte BytesN rendered hex — exactly 32 hex chars. Same
# reasoning as AGENT_ID_PATTERN: bound the path param at the router edge so a
# malformed id is a 422 with no RPC round-trip and no stack trace.
JOB_ID_HEX_PATTERN = r"^[0-9a-fA-F]{32}$"


# ── reads ───────────────────────────────────────────────────────
@router.get("/agent/{agent_id}", response_model=AgentRead)
async def read_agent(agent_id: str = Path(..., pattern=AGENT_ID_PATTERN)) -> AgentRead:
    """Read an Agent from AgentRegistry.get(id)."""
    try:

        async def _fetch() -> Any:
            return await asyncio.to_thread(
                sc.simulate_read,
                sc.contract_ids().agent_registry,
                "get",
                [sc.sym(agent_id)],
            )

        result = await rcache.get_or_set(f"agent:{agent_id}", READ_TTL_SECONDS, _fetch)
        return AgentRead(agent=result)
    except Exception as e:
        # Routine outcome for unknown ids (the simulate errors) — not a stack
        # trace event. Genuinely unexpected failures still surface via the 404
        # cause chain and RPC-layer logging.
        logger.warning("agent read failed for %s: %s", agent_id, e)
        raise HTTPException(404, "agent_read_failed") from e


class SyncResponse(BaseModel):
    synced: int


@router.post("/agents/sync", response_model=SyncResponse)
async def trigger_registry_sync() -> SyncResponse:
    """Run one registry-sync pass now — the FE's post-registration fast path.

    Advisory and idempotent (single-flight inside the service); the periodic
    loop remains the source of truth. Failure is a real answer here — the
    caller falls back to polling /api/agents until the next interval lands.
    """
    try:
        synced = await registry_sync.sync_once()
    except Exception as e:
        logger.warning("triggered registry sync failed: %s", e)
        raise HTTPException(503, "registry_sync_failed") from e
    return SyncResponse(synced=synced)


class AgentIdAvailability(BaseModel):
    available: bool
    # Stable strings — story 1.04's form maps them to field errors and the
    # 5.03 guide documents them: id_malformed · id_reserved · id_taken.
    reason: str | None = None
    message: str | None = None
    owner: str | None = None


@router.get("/agent-id-available/{agent_id}", response_model=AgentIdAvailability)
async def agent_id_available(agent_id: str = Path(..., max_length=64)) -> AgentIdAvailability:
    """Advisory pre-signature check for the registration form (id blur).

    Deliberately loose on the path param so a malformed id gets a friendly
    200 + reason instead of a bare 422 — the form shows the message inline.
    The check is advisory only: the contract's AlreadyExists is the final
    answer, and a race between this and submit is accepted (story 1.03).
    """
    if not re.fullmatch(AGENT_ID_PATTERN, agent_id):
        return AgentIdAvailability(
            available=False,
            reason="id_malformed",
            message="allowed: letters, digits and underscore, 1-32 chars",
        )
    if agent_id.startswith("agt_"):
        return AgentIdAvailability(
            available=False,
            reason="id_reserved",
            message="agt_ ids belong to the seeded catalog",
        )

    async def _resolve() -> AgentIdAvailability:
        # The same AgentRegistry.get read as read_agent, but under its own cache
        # key and with a never-raises contract: an unknown id (simulate errors)
        # and a transient read failure both mean "advisory: available" — the
        # chain's AlreadyExists is the real guard. Returning a value rather than
        # raising lets get_or_set positively cache BOTH outcomes, so repeated
        # blur checks of the same id — taken or free — and any concurrent burst
        # collapse to one Soroban read within the window (story 1.09).
        try:
            found = await asyncio.to_thread(
                sc.simulate_read,
                sc.contract_ids().agent_registry,
                "get",
                [sc.sym(agent_id)],
            )
        except Exception:
            return AgentIdAvailability(available=True)
        owner = found.get("owner") if isinstance(found, dict) else None
        return AgentIdAvailability(
            available=False,
            reason="id_taken",
            owner=owner if isinstance(owner, str) else None,
        )

    return await rcache.get_or_set(f"agentavail:{agent_id}", READ_TTL_SECONDS, _resolve)


@router.get("/reputation", response_model=ReputationBatch)
async def read_reputations() -> ReputationBatch:
    """Smoothed reputation for every registered agent, plus the routing
    floor and prior. Never fails: agents without on-chain evidence (or with
    the chain unreachable) come back as the prior, marked source="prior".
    """
    infos = await reputation_svc.fetch_reps([a.id for a in state.list_agents()])
    return ReputationBatch(
        reputations={aid: ReputationInfo(**info.model_dump()) for aid, info in infos.items()},
        floor_bps=settings.reputation_floor_bps,
        prior_bps=settings.reputation_prior_bps,
    )


# Declared BEFORE the dynamic /reputation/{agent_id} route — FastAPI matches
# routes in declaration order, so this must come first or "params" would be
# read as an agent id.
@router.get("/reputation/params", response_model=ReputationParams)
async def reputation_params() -> ReputationParams:
    """The reputation system's parameter set — pure config, no RPC call."""
    return ReputationParams(
        enabled=settings.reputation_enabled,
        prior_bps=settings.reputation_prior_bps,
        prior_weight_usdc=settings.reputation_prior_weight_usdc,
        floor_bps=settings.reputation_floor_bps,
        max_rating_weight_usdc=settings.reputation_max_rating_weight_usdc,
        read_ttl_seconds=settings.reputation_read_ttl_seconds,
        wilson_z=reputation_svc.WILSON_Z,
        epoch_seconds=reputation_svc.EPOCH_SECONDS,
        decay_bps_per_epoch=reputation_svc.DECAY_BPS_PER_EPOCH,
        max_decay_epochs=reputation_svc.MAX_DECAY_EPOCHS,
        contract_id=settings.stellar_reputation_ledger,
        network=settings.stellar_network,
    )


@router.get("/reputation/{agent_id}", response_model=ReputationInfo)
async def read_reputation(
    agent_id: str = Path(..., pattern=AGENT_ID_PATTERN),
) -> ReputationInfo:
    """Smoothed reputation for one agent — cached ReputationLedger.rep_state
    read with Bayesian prior smoothing; prior fallback on any failure.
    """
    info = await reputation_svc.fetch_rep(agent_id)
    return ReputationInfo(**info.model_dump())


@router.get("/attestation/{job_id_hex}", response_model=AttestationRead)
async def read_attestation(job_id_hex: str = Path(..., pattern=JOB_ID_HEX_PATTERN)) -> AttestationRead:
    """Read an on-chain Attestation by hex-encoded 16-byte job_id."""
    try:
        # The path pattern already guarantees 32 hex chars, so this decode
        # cannot fail and always yields exactly 16 bytes.
        jid = bytes.fromhex(job_id_hex)

        async def _fetch() -> Any:
            return await asyncio.to_thread(
                sc.simulate_read,
                sc.contract_ids().attestation_registry,
                "get",
                [sc.bytes16(jid)],
            )

        result = await rcache.get_or_set(f"attestation:{job_id_hex}", READ_TTL_SECONDS, _fetch)
        return AttestationRead(attestation=result)
    except Exception as e:
        # Same reasoning as read_agent: an unsealed job id is a routine miss,
        # not a stack-trace event — full tracebacks here bury the real errors.
        # The RPC layer logs the underlying failure with its own timing.
        logger.warning("attestation read failed for %s: %s", job_id_hex, e)
        raise HTTPException(400, "attestation_read_failed") from e


# ── writes (user signs via Freighter) ───────────────────────────
class RegisterAgentReq(BaseModel):
    owner: str = Field(..., pattern=r"^G[A-Z2-7]{55}$", description="G... address of the agent owner")
    # Soroban Symbol charset — anything outside it would only fail on-chain,
    # AFTER the user has already signed. Reject it at the API instead.
    agent_id: str = Field(..., pattern=AGENT_ID_PATTERN)
    name: str = Field(..., min_length=1, max_length=100)
    skills: list[Annotated[str, Field(pattern=AGENT_ID_PATTERN)]] = Field(default_factory=list, max_length=16)
    price_usdc: float = Field(..., gt=0, le=10_000, allow_inf_nan=False)


@router.post("/build/register-agent", response_model=XdrResponse)
async def build_register_agent(req: RegisterAgentReq) -> XdrResponse:
    """Build unsigned XDR for AgentRegistry.register. Owner signs via Freighter."""
    from stellar_sdk.exceptions import AccountNotFoundException

    # The seeded catalog owns the agt_ namespace (seed.py) — refuse it before
    # spending an RPC round-trip, and never silently rewrite an operator's id.
    if req.agent_id.startswith("agt_"):
        raise HTTPException(409, "id_reserved")

    # UX preflight: refuse a taken id BEFORE the wallet signs — a duplicate
    # would otherwise only surface as the on-chain AlreadyExists, after the
    # user already approved the transaction. The simulate read RAISES for an
    # unknown id (same contract behaviour read_agent relies on), so a read
    # that succeeds means the id exists. Read failures fall through open: the
    # chain stays the real guard, and the TOCTOU window between this check
    # and the user's submit is accepted.
    try:
        await asyncio.to_thread(
            sc.simulate_read,
            sc.contract_ids().agent_registry,
            "get",
            [sc.sym(req.agent_id)],
        )
    except Exception:
        pass  # unknown id (or transient read failure) — proceed to build
    else:
        raise HTTPException(409, "id_taken")

    try:
        from stellar_sdk import scval as _sv

        args = [
            sc.addr(req.owner),
            sc.sym(req.agent_id),
            _sv.to_string(req.name),
            _sv.to_vec([sc.sym(s) for s in req.skills]),
            sc.i128(sc.usdc_to_i128(req.price_usdc)),
        ]
        xdr = await asyncio.to_thread(
            sc.build_invoke_xdr,
            sc.contract_ids().agent_registry,
            "register",
            args,
            source=req.owner,
        )
        return XdrResponse(xdr=xdr)
    except AccountNotFoundException as e:
        # The build loads the owner account before anything else, so an
        # unfunded wallet dies here — and pre-hardening it surfaced as the
        # same opaque build_failed as a duplicate id or a bad charset
        # (1.01 audit finding). Name it so the FE can say "fund your wallet".
        logger.warning("register-agent build: owner account not found: %s", req.owner)
        raise HTTPException(400, "owner_account_unfunded") from e
    except Exception as e:
        logger.exception("register-agent build failed")
        raise HTTPException(400, "build_failed") from e


# ── agent management (owner signs; story 1.08) ──────────────────
async def _agent_exists(agent_id: str) -> bool:
    """Cached existence check on AgentRegistry.get(id), reusing read_agent's
    cache key so a management preflight adds no new Soroban amplification
    (story 1.09). The simulate raises for an unknown id → False. A seeded agt_
    id is not on-chain, so it correctly reports as not found here."""

    async def _fetch() -> Any:
        return await asyncio.to_thread(
            sc.simulate_read,
            sc.contract_ids().agent_registry,
            "get",
            [sc.sym(agent_id)],
        )

    try:
        await rcache.get_or_set(f"agent:{agent_id}", READ_TTL_SECONDS, _fetch)
    except Exception:
        return False
    return True


class UpdatePriceReq(BaseModel):
    owner: str = Field(..., pattern=r"^G[A-Z2-7]{55}$", description="G... address of the owner (the signer)")
    agent_id: str = Field(..., pattern=AGENT_ID_PATTERN)
    price_usdc: float = Field(..., gt=0, le=10_000, allow_inf_nan=False)


@router.post("/build/update-price", response_model=XdrResponse)
async def build_update_price(req: UpdatePriceReq) -> XdrResponse:
    """Build unsigned XDR for AgentRegistry.update_price. Owner signs via Freighter.

    The contract gates the write with agent.owner.require_auth(), so ownership is
    not re-checked here — the FE only offers the action to the owner, and a
    non-owner's signed tx fails on-chain. An unregistered id is refused as a
    plain 404 rather than surfacing as an opaque build failure after simulate.
    """
    from stellar_sdk.exceptions import AccountNotFoundException

    if not await _agent_exists(req.agent_id):
        raise HTTPException(404, "agent_not_found")

    try:
        args = [
            sc.sym(req.agent_id),
            sc.i128(sc.usdc_to_i128(req.price_usdc)),
        ]
        xdr = await asyncio.to_thread(
            sc.build_invoke_xdr,
            sc.contract_ids().agent_registry,
            "update_price",
            args,
            source=req.owner,
        )
        return XdrResponse(xdr=xdr)
    except AccountNotFoundException as e:
        logger.warning("update-price build: owner account not found: %s", req.owner)
        raise HTTPException(400, "owner_account_unfunded") from e
    except Exception as e:
        logger.exception("update-price build failed")
        raise HTTPException(400, "build_failed") from e


class SetActiveReq(BaseModel):
    owner: str = Field(..., pattern=r"^G[A-Z2-7]{55}$", description="G... address of the owner (the signer)")
    agent_id: str = Field(..., pattern=AGENT_ID_PATTERN)
    active: bool = Field(..., description="True relists the agent, False delists it")


@router.post("/build/set-active", response_model=XdrResponse)
async def build_set_active(req: SetActiveReq) -> XdrResponse:
    """Build unsigned XDR for AgentRegistry.set_active (delist / relist). Owner signs.

    Delisting is reversible — set_active(id, true) relists — and never deletes:
    the agent's on-chain record, history and reputation survive. Ownership is the
    contract's require_auth; an unregistered id is refused as a plain 404.
    """
    from stellar_sdk import scval as _sv
    from stellar_sdk.exceptions import AccountNotFoundException

    if not await _agent_exists(req.agent_id):
        raise HTTPException(404, "agent_not_found")

    try:
        args = [sc.sym(req.agent_id), _sv.to_bool(req.active)]
        xdr = await asyncio.to_thread(
            sc.build_invoke_xdr,
            sc.contract_ids().agent_registry,
            "set_active",
            args,
            source=req.owner,
        )
        return XdrResponse(xdr=xdr)
    except AccountNotFoundException as e:
        logger.warning("set-active build: owner account not found: %s", req.owner)
        raise HTTPException(400, "owner_account_unfunded") from e
    except Exception as e:
        logger.exception("set-active build failed")
        raise HTTPException(400, "build_failed") from e


class AuthorizeReq(BaseModel):
    payer: str = Field(..., pattern=r"^G[A-Z2-7]{55}$")
    # Same Symbol charset rule as registration — a bad id here also only
    # fails on-chain, after the payer signed the authorization envelope.
    agent_id: str = Field(..., pattern=AGENT_ID_PATTERN)
    max_amount_usdc: float = Field(..., gt=0, le=10_000, allow_inf_nan=False)
    ttl_seconds: int = Field(default=300, ge=30, le=3600)


@router.post("/build/authorize", response_model=AuthorizeXdrResponse)
async def build_authorize(req: AuthorizeReq) -> AuthorizeXdrResponse:
    """Build unsigned XDR for PaymentEscrow.authorize (x402 pre-auth)."""
    try:
        expires_at = int(time.time()) + req.ttl_seconds
        args = [
            sc.addr(req.payer),
            sc.sym(req.agent_id),
            sc.i128(sc.usdc_to_i128(req.max_amount_usdc)),
            sc.u64(expires_at),
        ]
        xdr = await asyncio.to_thread(
            sc.build_invoke_xdr,
            sc.contract_ids().payment_escrow,
            "authorize",
            args,
            source=req.payer,
        )
        return AuthorizeXdrResponse(xdr=xdr, expires_at=expires_at)
    except Exception as e:
        logger.exception("authorize build failed")
        raise HTTPException(400, "build_failed") from e


class SubmitReq(BaseModel):
    # A prepared invoke tx is a few KB of base64; 32 KiB is generous headroom
    # while keeping the endpoint from swallowing arbitrary payloads.
    signed_xdr: str = Field(..., min_length=1, max_length=32_768)


@router.post("/submit")
async def submit_signed(req: SubmitReq) -> dict:
    """Submit a Freighter-signed transaction XDR."""
    # Identify the transaction WITHOUT logging the envelope: signed XDR is
    # signature material, so only the derived hash and source account — both
    # public, both greppable against the explorer — go into the record.
    tx_hash, source = sc.envelope_identity(req.signed_xdr)
    try:
        result = await sc.submit_signed_xdr_async(req.signed_xdr)
    except Exception as e:
        logger.exception("signed xdr submit failed · tx_hash=%s source=%s", tx_hash, source)
        raise HTTPException(400, "submit_failed") from e
    # A successful submit may be a fresh registration — kick one sync pass so
    # the agent is listed within seconds instead of at the next interval
    # (BLO-12 AC). Fire-and-forget: the response never waits on it.
    if result.get("status") == "SUCCESS":
        registry_sync.kick()
    # Don't turn a FAILED tx into an HTTP error — the FE needs the hash + diagnostic.
    return result


# ── writes (backend signs with STELLAR_SIGNING_KEY) ──────────────
class ChargeReq(BaseModel):
    auth_id_hex: str = Field(..., pattern=r"^[0-9a-fA-F]{32}$")
    amount_usdc: float = Field(..., gt=0, le=10_000, allow_inf_nan=False)
    job_id_hex: str = Field(..., pattern=r"^[0-9a-fA-F]{32}$")


@router.post("/server/charge", dependencies=[Depends(require_api_key)])
async def server_charge(req: ChargeReq) -> dict:
    """Backend-signed PaymentEscrow.charge (the backend is the `settler` role)."""
    if not settings.stellar_signing_key:
        raise HTTPException(503, "backend signing key not configured")
    if not (0 < req.amount_usdc <= settings.max_charge_usdc):
        raise HTTPException(400, "amount_exceeds_charge_cap")
    try:
        aid = bytes.fromhex(req.auth_id_hex)
        jid = bytes.fromhex(req.job_id_hex)
        if len(aid) != 16 or len(jid) != 16:
            raise ValueError("ids must be 32 hex chars")

        caller = sc._signer_keypair().public_key

        args = [
            sc.addr(caller),
            sc.bytes16(aid),
            sc.i128(sc.usdc_to_i128(req.amount_usdc)),
            sc.bytes16(jid),
        ]
        return await sc.invoke_with_server_key_async(
            sc.contract_ids().payment_escrow,
            "charge",
            args,
        )
    except Exception as e:
        # A money-path stack trace has to be tie-able to the on-chain
        # transaction it belongs to, or it is unusable during an incident.
        # Ids and amount only — never the signing key or its derived secret.
        logger.exception(
            "server charge failed · auth_id=%s job_id=%s amount_usdc=%s",
            req.auth_id_hex,
            req.job_id_hex,
            req.amount_usdc,
        )
        raise HTTPException(400, "charge_failed") from e


class SealReq(BaseModel):
    job_id_hex: str = Field(..., pattern=r"^[0-9a-fA-F]{32}$")
    orchestrator: str = Field(..., pattern=r"^G[A-Z2-7]{55}$")  # G-address of the workflow owner
    intent_hash_hex: str = Field(..., pattern=r"^[0-9a-fA-F]{64}$")
    agents: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(..., max_length=32)
    receipts_hex: list[Annotated[str, Field(pattern=r"^[0-9a-fA-F]{32}$")]] = Field(..., max_length=32)
    total_spent_usdc: float = Field(..., ge=0, le=100_000, allow_inf_nan=False)


@router.post("/server/seal", dependencies=[Depends(require_api_key)])
async def server_seal(req: SealReq) -> dict:
    """Backend-signed AttestationRegistry.seal (backend is the `sealer` role)."""
    if not settings.stellar_signing_key:
        raise HTTPException(503, "backend signing key not configured")
    try:
        from stellar_sdk import scval as _sv

        caller = sc._signer_keypair().public_key
        jid = bytes.fromhex(req.job_id_hex)
        ih = bytes.fromhex(req.intent_hash_hex)
        if len(jid) != 16 or len(ih) != 32:
            raise ValueError("bad id lengths")

        receipts = []
        for rh in req.receipts_hex:
            rb = bytes.fromhex(rh)
            if len(rb) != 16:
                raise ValueError(f"bad receipt_id: {rh}")
            receipts.append(sc.bytes16(rb))

        args = [
            sc.addr(caller),
            sc.bytes16(jid),
            sc.addr(req.orchestrator),
            sc.bytes32(ih),
            _sv.to_vec([sc.sym(a) for a in req.agents]),
            _sv.to_vec(receipts),
            sc.i128(sc.usdc_to_i128(req.total_spent_usdc)),
        ]
        return await sc.invoke_with_server_key_async(
            sc.contract_ids().attestation_registry,
            "seal",
            args,
        )
    except Exception as e:
        # Same rule as charge: enough to find the attestation on-chain
        # (job id, the orchestrator paying for it, the sealed total), nothing
        # that could reconstruct a key.
        logger.exception(
            "server seal failed · job_id=%s orchestrator=%s agents=%d total_spent_usdc=%s",
            req.job_id_hex,
            req.orchestrator,
            len(req.agents),
            req.total_spent_usdc,
        )
        raise HTTPException(400, "seal_failed") from e


# ── handy: new 16-byte id ──────────────────────────────────────
@router.get("/new-id", response_model=NewIdResponse)
async def new_id() -> NewIdResponse:
    """Produce a random 16-byte id (hex) — useful for job_id / auth_id."""
    return NewIdResponse(id_hex=secrets.token_hex(16))
