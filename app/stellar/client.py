"""
Thin wrapper around stellar-sdk for talking to the four Orizon contracts.

Design: everything reads via Soroban RPC (no signing). For *writes* we expose
two helpers:

  - `build_invoke_xdr(...)` → returns an unsigned base64 XDR that the frontend
    hands to Freighter for the user to sign. The user's wallet is the payer.

  - `invoke_with_server_key(...)` → signs with the backend's STELLAR_SIGNING_KEY
    (the `settler` / `sealer` / `scorer` role). Used for charge / seal / rate.

All amounts are i128 with Stellar's 7-decimal convention (0.012 USDC → 120000).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from stellar_sdk import (
    Address,
    Durability,
    Keypair,
    Network,
    SorobanServer,
    TransactionBuilder,
    scval,
)
from stellar_sdk.client.requests_client import RequestsClient
from stellar_sdk.exceptions import PrepareTransactionException
from stellar_sdk.soroban_rpc import GetTransactionStatus, SendTransactionStatus
from stellar_sdk.xdr import SCVal, SCValType

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContractIds:
    agent_registry: str
    reputation_ledger: str
    payment_escrow: str
    attestation_registry: str
    asset_sac: str


@lru_cache(maxsize=1)
def contract_ids() -> ContractIds:
    return ContractIds(
        agent_registry=settings.stellar_agent_registry,
        reputation_ledger=settings.stellar_reputation_ledger,
        payment_escrow=settings.stellar_payment_escrow,
        attestation_registry=settings.stellar_attestation_registry,
        asset_sac=settings.stellar_asset_sac,
    )


@lru_cache(maxsize=1)
def network_passphrase() -> str:
    return settings.stellar_network_passphrase or Network.TESTNET_NETWORK_PASSPHRASE


def explorer_network() -> str:
    """stellar.expert network segment for the configured network."""
    return "public" if settings.stellar_network in ("mainnet", "public") else settings.stellar_network


# ── observability ───────────────────────────────────────────────────────
# Every Soroban round-trip is timed and emitted in the same shape as
# `app/pdax/observability.py` — operation, target, latency, outcome — so a
# "the app is slow" report resolves to a specific call instead of a shrug.
#
# LEVEL POLICY (deliberately asymmetric between the traffic classes):
#
#   reads    A dashboard metrics poll fans out up to twelve `simulate_read`
#            calls at once, each of which is TWO sequential RPC hops, so an
#            INFO line per success would bury every other record. Successes
#            therefore log at DEBUG (off in production, one env var away when
#            you need them) and are already aggregated: one line per read
#            carrying both hops' timings, not one per hop.
#            A read over SLOW_READ_MS logs at WARNING — that is the signal
#            that the rate-limited SDF public RPC (see render.yaml) is
#            throttling us, and it is the precursor to the bounded 8-thread
#            executor in app/main.py saturating and stalling unrelated
#            routes. Rare by construction, so a line each is affordable.
#            Failures log at ERROR: every one of them is a 4xx/5xx a user saw.
#
#   submits  Rare, on the money path, and each one moves value on-chain, so
#            successes are worth an INFO line (`notable=True`). Slow/failed
#            submits follow the same WARNING/ERROR rule against the larger
#            SLOW_SUBMIT_MS budget.
#
#   polls    Duration is dominated by ledger close time (~5s), not by RPC
#            health, so they get their own much larger threshold: warning at
#            SLOW_POLL_MS means "this transaction is taking abnormally long
#            to confirm", not "the RPC is slow".
#
# Module-level constants, not settings: these are diagnostic thresholds tuned
# to the timeout profiles in `_server()`, not per-deployment knobs.
SLOW_READ_MS = 1_500.0
SLOW_SUBMIT_MS = 5_000.0
SLOW_POLL_MS = 20_000.0


def _ms_since(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


def _short(value: str) -> str:
    """Abbreviate a 56-char Stellar id/address to something greppable."""
    return f"{value[:6]}…{value[-4:]}" if len(value) > 14 else value


def _contract_label(contract_id: str) -> str:
    """Friendly name for one of our four contracts; abbreviated id otherwise."""
    try:
        ids = contract_ids()
    except Exception:  # config not loaded (shouldn't happen) — don't break logging
        return _short(contract_id)
    for name, value in vars(ids).items():
        if value == contract_id:
            return name
    return _short(contract_id)


def _log_rpc(
    op: str,
    target: str,
    latency_ms: float,
    span: dict[str, Any],
    *,
    slow_ms: float,
    notable: bool = False,
    error: str | None = None,
) -> None:
    """Emit exactly one structured line for a Soroban RPC interaction."""
    detail = " ".join(f"{k}={v}" for k, v in span.items())
    tail = f" · {detail}" if detail else ""
    if error is not None:
        logger.error("[stellar.rpc] %s %s failed in %.0fms%s: %s", op, target, latency_ms, tail, error)
    elif latency_ms >= slow_ms:
        logger.warning("[stellar.rpc] %s %s slow: %.0fms%s", op, target, latency_ms, tail)
    else:
        logger.log(
            logging.INFO if notable else logging.DEBUG,
            "[stellar.rpc] %s %s ok in %.0fms%s",
            op,
            target,
            latency_ms,
            tail,
        )


@contextmanager
def _rpc_span(op: str, target: str, *, slow_ms: float, notable: bool = False) -> Iterator[dict[str, Any]]:
    """Time one RPC interaction and log its outcome, exactly once, either way.

    Yields a mutable dict the body annotates (`stage=`, `tx=`, per-hop
    timings); its items are appended to the line as `k=v` pairs, so a failure
    says how far the call got before it died.
    """
    span: dict[str, Any] = {}
    started = time.monotonic()
    try:
        yield span
    except Exception as e:
        _log_rpc(op, target, _ms_since(started), span, slow_ms=slow_ms, error=f"{type(e).__name__}: {e}")
        raise
    _log_rpc(op, target, _ms_since(started), span, slow_ms=slow_ms, notable=notable)


_thread_local = threading.local()


def _server(*, submit: bool = False) -> SorobanServer:
    """Return this thread's cached SorobanServer for the given traffic class.

    stellar-sdk 13.x's SorobanServer defaults to RequestsClient, which holds a
    requests.Session — and requests does not guarantee Session thread safety
    (response cookie-jar updates are unsynchronized). Sharing one instance
    across worker threads is therefore unsafe, so we cache one per thread:
    asyncio.to_thread reuses a small executor pool, so each thread still keeps
    its TCP+TLS connections alive across calls.

    Two timeout profiles (the SDK default — post_timeout 33s × 3 retries
    ≈ 99s worst case — can pin asyncio.to_thread workers for minutes):

      - read (default): simulate/load_account view calls sit on the hot
        request path, often twelve-wide behind a ~2.5s caller deadline. Cap
        hard at 5s with no retry so a misbehaving RPC fails fast instead of
        holding a worker thread ~60s.
      - submit: transaction submission is rarer and worth patience — 15s cap
        with a single retry.
    """
    attr = "submit_server" if submit else "read_server"
    server = getattr(_thread_local, attr, None)
    if server is None:
        client = (
            RequestsClient(request_timeout=8, post_timeout=15, num_retries=1)
            if submit
            else RequestsClient(request_timeout=5, post_timeout=5, num_retries=0)
        )
        server = SorobanServer(settings.stellar_rpc_url, client=client)
        setattr(_thread_local, attr, server)
    return server


# ── reads ──────────────────────────────────────────────────────────────
def simulate_read(
    contract_id: str,
    function_name: str,
    args: list[Any] | None = None,
    source: str | None = None,
) -> Any:
    """
    Simulate a view-style call — no signature, no fees, no state change.

    `args` must be stellar_sdk.scval values (built via `scval.to_*`).
    """
    server = _server()
    src_addr = source or settings.stellar_admin_address
    if not src_addr:
        raise RuntimeError("no source address; set STELLAR_ADMIN_ADDRESS")

    # One span for the whole read, annotated with the per-hop split: this call
    # is two sequential blocking round-trips, and when it goes slow the split
    # is what says whether the RPC is throttling us on load_account or on
    # simulate_transaction.
    with _rpc_span("read", f"{_contract_label(contract_id)}.{function_name}", slow_ms=SLOW_READ_MS) as span:
        span["src"] = _short(src_addr)
        span["stage"] = "load_account"
        hop = time.monotonic()
        account = server.load_account(src_addr)
        span["load_ms"] = f"{_ms_since(hop):.0f}"

        tx = (
            TransactionBuilder(
                source_account=account,
                network_passphrase=network_passphrase(),
                base_fee=100,
            )
            .append_invoke_contract_function_op(
                contract_id=contract_id,
                function_name=function_name,
                parameters=args or [],
            )
            .set_timeout(30)
            .build()
        )
        span["stage"] = "simulate"
        hop = time.monotonic()
        sim = server.simulate_transaction(tx)
        span["sim_ms"] = f"{_ms_since(hop):.0f}"
        if sim.error:
            raise RuntimeError(f"simulate failed: {sim.error}")
        span["stage"] = "ok"
    # Latest successful result is in `results[0].xdr` (base64). For convenience,
    # decode with scval helpers at the call site.
    if not sim.results:
        return None
    return _to_jsonable(scval.to_native(sim.results[0].xdr))


def _to_jsonable(value: Any) -> Any:
    """Recursively convert scval natives to JSON-safe values.

    `scval.to_native` yields stellar_sdk `Address` objects and raw `bytes`
    (BytesN fields) — neither survives FastAPI's JSON encoding, which happens
    outside router try/except blocks and so used to surface as a bare 500.
    """
    if isinstance(value, Address):
        return value.address
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


# `DataKey::Scorer` as ReputationLedger writes it into instance storage: a
# `#[contracttype]` unit variant encodes as a one-element vec holding the
# variant's name as a symbol (contract/reputation-ledger/src/lib.rs).
_SCORER_STORAGE_KEY = scval.to_vec([scval.to_symbol("Scorer")])


def _instance_storage_address(entry_xdr: str, key: SCVal) -> str | None:
    """The address stored under `key` in a contract-instance ledger entry.

    `entry_xdr` is the base64 `LedgerEntryData` that getLedgerEntries returns
    for a contract's instance key. None when the instance's storage holds no
    such key — a definite answer, read off the chain. Anything that is not a
    contract instance, or a value under `key` that is not an address, raises
    ValueError instead: an entry this cannot decode must never read as "no
    value stored".
    """
    from stellar_sdk import xdr as _xdr

    data = _xdr.LedgerEntryData.from_xdr(entry_xdr)
    contract_data = data.contract_data
    if contract_data is None or contract_data.val.instance is None:
        raise ValueError("ledger entry is not a contract instance")
    storage = contract_data.val.instance.storage
    for item in storage.sc_map if storage is not None else []:
        if item.key == key:
            return scval.from_address(item.val).address
    return None


def ledger_scorer(ledger_id: str) -> str | None:
    """The address ReputationLedger `ledger_id` stores as its Scorer.

    `submit` accepts a rating only from that address, and the contract has no
    view that returns it, so it is read straight off the contract's instance
    ledger entry: one getLedgerEntries round trip on the read profile (5 s, no
    retry) — no source account, no simulation, nothing signed.

    None means the chain answered and holds no Scorer: no contract instance
    lives at `ledger_id` on this network, or its storage has no Scorer key.
    Anything else raises — an RPC failure, or an entry that does not decode —
    because "could not read" must never be mistaken for "read, and absent".
    """
    server = _server()
    with _rpc_span("read", f"{_contract_label(ledger_id)}.Scorer", slow_ms=SLOW_READ_MS) as span:
        span["stage"] = "get_ledger_entries"
        entry = server.get_contract_data(
            ledger_id,
            SCVal(SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE),
            Durability.PERSISTENT,
        )
        span["stage"] = "decode"
        scorer = None if entry is None else _instance_storage_address(entry.xdr, _SCORER_STORAGE_KEY)
        span["stage"] = "ok"
    return scorer


@lru_cache(maxsize=1)
def _signer_keypair() -> Keypair:
    """
    Build a Keypair from STELLAR_SIGNING_KEY, accepting either:
      - an S… secret key (56 chars), OR
      - a 12/24-word BIP-39 mnemonic seed phrase (words separated by spaces).

    Memoized: the signing key is immutable at runtime, so we derive once.
    (lru_cache does not cache exceptions, so an unset key keeps raising.)
    """
    secret = settings.stellar_signing_key or ""
    secret = secret.strip()
    if not secret:
        raise RuntimeError("STELLAR_SIGNING_KEY is empty")

    words = secret.split()
    if len(words) >= 12:
        try:
            return Keypair.from_mnemonic_phrase(" ".join(words))
        except Exception as e:
            raise RuntimeError(f"STELLAR_SIGNING_KEY looks like a mnemonic but is invalid: {e}") from e
    try:
        return Keypair.from_secret(secret)
    except Exception as e:
        raise RuntimeError(f"STELLAR_SIGNING_KEY must be an S… secret or a 12/24-word mnemonic ({e})") from e


def signer_public_key() -> str:
    """Public key (G…) of the backend signing keypair, from the cached Keypair."""
    return _signer_keypair().public_key


# ── writes (backend-signed) ─────────────────────────────────────────────
# Submit/poll budget: same ~30s total as the old 30 × 1s loop, but the async
# poll waits with mild exponential backoff (1s → 2s → 4s capped).
_POLL_BUDGET_SECONDS = 30.0
_POLL_MAX_DELAY_SECONDS = 4.0

# The head of a simulation error when the invoked contract itself returned
# one of its `#[contracterror]` values: "HostError: Error(Contract, #7)",
# followed by the diagnostic event log. Anchored to the head on purpose — the
# event log below it can quote other errors, and only the head is the one the
# call failed with.
_CONTRACT_ERROR_HEAD = re.compile(r"\s*HostError: Error\(Contract, #(\d+)\)")


class ContractError(RuntimeError):
    """A backend-signed call the contract itself rejected, with its error code.

    The code is the discriminant of the contract's own `Error` enum, carried
    as data so a caller can name the rejection (ReputationLedger's 1 is
    `Unauthorized`, 7 is `Replay`) without parsing — or echoing — the
    simulation's error text, which runs to the whole diagnostic event log.
    Still a RuntimeError, so every existing `except` keeps catching it.
    """

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _contract_error_code(simulation_error: str | None) -> int | None:
    """The contract error code heading a simulation error, or None."""
    match = _CONTRACT_ERROR_HEAD.match(simulation_error or "")
    return int(match.group(1)) if match else None


def _send_server_signed(
    contract_id: str,
    function_name: str,
    args: list[Any],
) -> str:
    """Build, prepare, sign (backend key) and send; returns the pending tx hash."""
    kp = _signer_keypair()
    server = _server(submit=True)
    label = f"{_contract_label(contract_id)}.{function_name}"

    with _rpc_span("submit", label, slow_ms=SLOW_SUBMIT_MS, notable=True) as span:
        span["signer"] = _short(kp.public_key)
        span["stage"] = "load_account"
        account = server.load_account(kp.public_key)

        tx = (
            TransactionBuilder(
                source_account=account,
                network_passphrase=network_passphrase(),
                base_fee=100,
            )
            .append_invoke_contract_function_op(
                contract_id=contract_id,
                function_name=function_name,
                parameters=args,
            )
            .set_timeout(30)
            .build()
        )
        span["stage"] = "prepare"
        try:
            tx = server.prepare_transaction(tx)
        except PrepareTransactionException as e:
            detail = e.simulate_transaction_response.error
            code = _contract_error_code(detail)
            if code is not None:
                raise ContractError(f"prepare failed: {detail}", code) from e
            raise RuntimeError(f"prepare failed: {detail}") from e
        tx.sign(kp)

        span["stage"] = "send"
        sent = server.send_transaction(tx)
        if sent.status != SendTransactionStatus.PENDING:
            raise RuntimeError(f"submit failed: {sent.error_result_xdr}")
        span["stage"] = "pending"
        span["tx"] = sent.hash
    return sent.hash


def _get_transaction(tx_hash: str) -> Any:
    return _server(submit=True).get_transaction(tx_hash)


def _finalize_invoke(status: Any, tx_hash: str) -> dict[str, Any]:
    rv = _extract_return_value(status.result_meta_xdr)
    if isinstance(rv, (bytes, bytearray)):
        rv = rv.hex()
    return {
        "hash": tx_hash,
        "status": status.status.value,
        "ledger": status.ledger,
        "result": rv,
    }


def _log_poll(tx_hash: str, started: float, polls: int, outcome: str, *, timed_out: bool = False) -> None:
    """One line per settled/abandoned transaction — see the level policy above."""
    span = {"polls": polls, "status": outcome}
    _log_rpc(
        "poll",
        _short(tx_hash),
        _ms_since(started),
        span,
        slow_ms=SLOW_POLL_MS,
        notable=True,
        # A timeout means we submitted value on-chain and then lost track of
        # it — the one polling outcome that always deserves an ERROR.
        error=f"unconfirmed after {_POLL_BUDGET_SECONDS:.0f}s" if timed_out else None,
    )


def _poll_final_sync(tx_hash: str, finalize: Any) -> dict[str, Any]:
    """Blocking poll for final tx status (sync callers only — pins a thread)."""
    started = time.monotonic()
    for polls in range(1, int(_POLL_BUDGET_SECONDS) + 1):
        status = _get_transaction(tx_hash)
        if status.status in (GetTransactionStatus.SUCCESS, GetTransactionStatus.FAILED):
            _log_poll(tx_hash, started, polls, status.status.value)
            return finalize(status, tx_hash)
        time.sleep(1)
    _log_poll(tx_hash, started, int(_POLL_BUDGET_SECONDS), "timeout", timed_out=True)
    return {"hash": tx_hash, "status": "timeout"}


async def _poll_final(tx_hash: str, finalize: Any) -> dict[str, Any]:
    """Poll for final tx status with the WAIT on the event loop.

    Each get_transaction RPC runs in a worker thread, but the sleeps between
    polls are `await asyncio.sleep` — no thread is pinned across the ~30s
    budget.
    """
    started = time.monotonic()
    deadline = started + _POLL_BUDGET_SECONDS
    delay = 1.0
    polls = 0
    while True:
        status = await asyncio.to_thread(_get_transaction, tx_hash)
        polls += 1
        if status.status in (GetTransactionStatus.SUCCESS, GetTransactionStatus.FAILED):
            _log_poll(tx_hash, started, polls, status.status.value)
            return finalize(status, tx_hash)
        if time.monotonic() + delay > deadline:
            _log_poll(tx_hash, started, polls, "timeout", timed_out=True)
            return {"hash": tx_hash, "status": "timeout"}
        await asyncio.sleep(delay)
        delay = min(delay * 2.0, _POLL_MAX_DELAY_SECONDS)


def invoke_with_server_key(
    contract_id: str,
    function_name: str,
    args: list[Any],
) -> dict[str, Any]:
    """Sign + submit a contract invocation with the backend's STELLAR_SIGNING_KEY.

    Sync variant — polls with time.sleep. On the event loop prefer
    `invoke_with_server_key_async`, which waits between polls on the loop.
    """
    tx_hash = _send_server_signed(contract_id, function_name, args)
    return _poll_final_sync(tx_hash, _finalize_invoke)


async def invoke_with_server_key_async(
    contract_id: str,
    function_name: str,
    args: list[Any],
) -> dict[str, Any]:
    """Async invoke_with_server_key: submit in a worker thread, wait on the loop."""
    tx_hash = await asyncio.to_thread(_send_server_signed, contract_id, function_name, args)
    return await _poll_final(tx_hash, _finalize_invoke)


def _submit_rating_args(
    agent_id: str,
    job_id: bytes,
    rating_0_to_100: int,
    weight_stroops: int,
    payer: str,
    kind: str,
) -> list[Any]:
    return [
        addr(signer_public_key()),
        sym(agent_id),
        bytes16(job_id),
        u32(rating_0_to_100),
        i128(weight_stroops),
        addr(payer),
        sym(kind),
    ]


def submit_rating(
    agent_id: str,
    job_id: bytes,
    rating_0_to_100: int,
    weight_stroops: int,
    payer: str,
    kind: str = "auto",
) -> dict[str, Any]:
    """ReputationLedger.submit signed by the backend scorer key.

    Sync variant — polls with time.sleep. On the event loop prefer
    `submit_rating_async`, which waits between polls on the loop.
    """
    return invoke_with_server_key(
        contract_ids().reputation_ledger,
        "submit",
        _submit_rating_args(agent_id, job_id, rating_0_to_100, weight_stroops, payer, kind),
    )


async def submit_rating_async(
    agent_id: str,
    job_id: bytes,
    rating_0_to_100: int,
    weight_stroops: int,
    payer: str,
    kind: str = "auto",
) -> dict[str, Any]:
    """Async submit_rating: submit in a worker thread, wait on the loop.

    Same call and return shape as `submit_rating`, but built on
    `invoke_with_server_key_async` so the ~30s status poll never pins an
    executor thread.
    """
    return await invoke_with_server_key_async(
        contract_ids().reputation_ledger,
        "submit",
        _submit_rating_args(agent_id, job_id, rating_0_to_100, weight_stroops, payer, kind),
    )


# ── writes (user-signed via Freighter) ──────────────────────────────────
def build_invoke_xdr(
    contract_id: str,
    function_name: str,
    args: list[Any],
    source: str,
) -> str:
    """
    Build an UNSIGNED, prepared transaction XDR for the frontend to hand to
    Freighter. After Freighter returns the signed XDR, submit it with
    `submit_signed_xdr(signed_xdr)`.
    """
    server = _server(submit=True)
    label = f"{_contract_label(contract_id)}.{function_name}"
    # Two round-trips like a read, but user-initiated and one-per-click, so
    # successes are worth an INFO line.
    with _rpc_span("build", label, slow_ms=SLOW_SUBMIT_MS, notable=True) as span:
        span["src"] = _short(source)
        span["stage"] = "load_account"
        account = server.load_account(source)
        tx = (
            TransactionBuilder(
                source_account=account,
                network_passphrase=network_passphrase(),
                base_fee=100,
            )
            .append_invoke_contract_function_op(
                contract_id=contract_id,
                function_name=function_name,
                parameters=args,
            )
            .set_timeout(300)
            .build()
        )
        span["stage"] = "prepare"
        prepared = server.prepare_transaction(tx)
        span["stage"] = "ok"
    return prepared.to_xdr()


def envelope_identity(signed_xdr: str) -> tuple[str, str]:
    """Secret-free identifiers for a signed envelope: (tx_hash, source G… address).

    Never raises, so it is safe to call purely to label a log line. Returns
    only these two *derived, public* values — never any slice of the XDR
    itself, which carries the user's signature material. Undecodable input
    yields placeholders rather than an exception.
    """
    try:
        from stellar_sdk import TransactionEnvelope

        env = TransactionEnvelope.from_xdr(signed_xdr, network_passphrase())
        return env.hash_hex(), env.transaction.source.account_id
    except Exception:
        return "unparseable", "unknown"


def _send_signed_xdr(signed_xdr: str) -> str:
    """Decode + broadcast a user-signed envelope; returns the pending tx hash."""
    from stellar_sdk import TransactionEnvelope

    server = _server(submit=True)
    try:
        env = TransactionEnvelope.from_xdr(signed_xdr, network_passphrase())
    except Exception as e:
        logger.warning("[stellar.submit] bad XDR: %s", e)
        raise RuntimeError(f"bad signed XDR (likely wrong networkPassphrase or malformed): {e}") from e

    # The envelope's own hash and source account identify the transaction; the
    # XDR itself is never logged (it carries the user's signature payload).
    # Derived defensively — a label must never turn a good submit into a 400.
    tx_hash, src = envelope_identity(signed_xdr)
    with _rpc_span("submit-signed", _short(tx_hash), slow_ms=SLOW_SUBMIT_MS, notable=True) as span:
        span["src"] = _short(src)
        sent = server.send_transaction(env)
        if sent.status != SendTransactionStatus.PENDING:
            detail = f"status={sent.status} error={getattr(sent, 'error_result_xdr', None)} hash={sent.hash}"
            logger.error("[stellar.submit] send failed: %s", detail)
            raise RuntimeError(f"submit failed ({detail})")
        span["stage"] = "pending"
    return sent.hash


def _finalize_submit(status: Any, tx_hash: str) -> dict[str, Any]:
    rv = _extract_return_value(status.result_meta_xdr)
    if isinstance(rv, (bytes, bytearray)):
        rv = rv.hex()
    diag = _extract_diagnostics(status)
    if status.status == GetTransactionStatus.FAILED:
        logger.error("[stellar.submit] tx %s FAILED · %s", tx_hash, diag)
    return {
        "hash": tx_hash,
        "status": status.status.value,
        "ledger": status.ledger,
        "return_value": rv,
        "diagnostic": diag,
        "explorer": f"https://stellar.expert/explorer/{explorer_network()}/tx/{tx_hash}",
    }


def submit_signed_xdr(signed_xdr: str) -> dict[str, Any]:
    """Submit a user-signed (via Freighter) prepared transaction.

    Sync variant — polls with time.sleep. On the event loop prefer
    `submit_signed_xdr_async`, which waits between polls on the loop.
    """
    tx_hash = _send_signed_xdr(signed_xdr)
    return _poll_final_sync(tx_hash, _finalize_submit)


async def submit_signed_xdr_async(signed_xdr: str) -> dict[str, Any]:
    """Async submit_signed_xdr: broadcast in a worker thread, wait on the loop."""
    tx_hash = await asyncio.to_thread(_send_signed_xdr, signed_xdr)
    return await _poll_final(tx_hash, _finalize_submit)


def _extract_diagnostics(status: Any) -> str:
    """Summarize failure reasons from diagnostic events + result xdr."""
    bits: list[str] = []
    undecodable = 0
    try:
        from stellar_sdk import xdr as _xdr

        for ev_xdr in getattr(status, "diagnostic_events_xdr", None) or []:
            try:
                ev = _xdr.DiagnosticEvent.from_xdr(ev_xdr)
                # Re-stringify the interesting parts without crashing on exotic shapes.
                s = str(ev)
                # Keep the message terse — drop internal whitespace.
                s = " ".join(s.split())
                if "Error(" in s or "error" in s.lower():
                    bits.append(s[:360])
            except Exception as e:
                # Diagnostics-of-diagnostics: one exotic event must not hide the
                # rest, but it must not vanish either. The per-event reason goes
                # to DEBUG (it repeats per event and is rarely actionable); the
                # COUNT is folded into the returned summary, which the caller
                # logs at ERROR and returns to the client — so a systematically
                # undecodable event stream is always visible.
                undecodable += 1
                logger.debug("[stellar.submit] diagnostic event decode failed: %s", e)
                continue
        if undecodable:
            bits.append(f"{undecodable} undecodable diagnostic event(s)")
        if not bits:
            rxdr = getattr(status, "result_xdr", None)
            if rxdr:
                bits.append(f"result_xdr={rxdr[:120]}")
    except Exception as e:
        bits.append(f"(diag parse: {e})")
    return " | ".join(bits) if bits else "no diagnostic events"


def _extract_return_value(result_meta_xdr: str | None) -> Any:
    """Pull the Soroban contract return value out of a transaction's meta XDR.

    stellar-sdk 13.x no longer exposes `GetTransactionResponse.return_value`;
    we parse `result_meta_xdr` ourselves.
    """
    if not result_meta_xdr:
        return None
    try:
        from stellar_sdk import xdr as _xdr

        meta = _xdr.TransactionMeta.from_xdr(result_meta_xdr)
        # TransactionMetaV3 has a `soroban_meta.return_value`; v4 has a similar field.
        for attr in ("v3", "v4"):
            v = getattr(meta, attr, None)
            if v is None:
                continue
            soroban = getattr(v, "soroban_meta", None)
            if soroban and getattr(soroban, "return_value", None) is not None:
                return scval.to_native(soroban.return_value)
    except Exception as e:
        logger.warning("[stellar.submit] meta decode failed: %s", e)
    return None


# ── helpers for arg encoding ────────────────────────────────────────────
def sym(s: str) -> SCVal:
    return scval.to_symbol(s)


def addr(a: str) -> SCVal:
    return scval.to_address(Address(a))


def i128(v: int) -> SCVal:
    return scval.to_int128(v)


def u64(v: int) -> SCVal:
    return scval.to_uint64(v)


def u32(v: int) -> SCVal:
    return scval.to_uint32(v)


def bytes16(b: bytes) -> SCVal:
    assert len(b) == 16
    return scval.to_bytes(b)


def bytes32(b: bytes) -> SCVal:
    assert len(b) == 32
    return scval.to_bytes(b)


def usdc_to_i128(amount_usdc: float) -> int:
    """0.012 → 120_000 (Stellar uses 7 decimals)."""
    return round(amount_usdc * 10_000_000)
