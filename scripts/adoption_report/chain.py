"""Read-only testnet reads: Soroban RPC and Horizon, the only authorities here.

Nothing here builds a transaction that is ever signed or sent, and nothing
needs a secret. A contract VIEW (`AgentRegistry.owner_of`, `get`,
`PaymentEscrow.authorization`) is read by `simulateTransaction` on an envelope
built from a placeholder source at sequence 0 — the backend's
`simulate_read(load_source=False)` recipe (`app/stellar/client.py`), which is
proven to answer identically for a never-funded source, re-stated here so this
tool never imports `app` at runtime.

A transaction's fate comes from RPC `getTransaction`, whose answer carries
both the envelope (what was invoked, with which arguments) and the contract
events it emitted. Soroban RPC keeps about seven days of history; an older
hash is asked of Horizon, which keeps the status and the envelope but, since
Horizon 23, no longer serves the result meta the events live in. So a
`TxRecord` read from Horizon has `events=None` — "not available", which the
checks treat differently from "none were emitted".

All traffic goes through one injected `httpx.Client`, so the hermetic suite
answers it with a fake RPC and a fake Horizon (`fakes.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from stellar_sdk import Account, Address, StrKey, TransactionBuilder, scval
from stellar_sdk import xdr as stellar_xdr

from .config import TESTNET_PASSPHRASE
from .retry import RETRYABLE_STATUS, RetryableStatus, RetryPolicy, retry_after_seconds

# A valid account strkey (the all-zero key) used as the source of every
# simulation. Simulation never checks the source exists or its sequence, so no
# real account — and no secret — is involved.
SIMULATION_SOURCE = "GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF"


class RpcError(Exception):
    """The RPC answered with a JSON-RPC error: a real answer, not a timeout."""


class SimulationError(Exception):
    """The contract call failed in simulation (no such agent, no such function)."""


@dataclass(frozen=True)
class ChainEvent:
    contract_id: str
    topics: list[Any]
    value: Any


@dataclass(frozen=True)
class Invocation:
    contract_id: str
    function: str
    args: list[Any]


@dataclass(frozen=True)
class TxRecord:
    """What the ledger says about one transaction hash, and who said it."""

    tx_hash: str
    status: str  # SUCCESS | FAILED | NOT_FOUND
    ledger: int | None
    source: str  # "rpc" | "horizon" | "none"
    invocations: list[Invocation] = field(default_factory=list)
    # None: the events are not available from the source that answered
    # (Horizon). []: the source answered and the transaction emitted none.
    events: list[ChainEvent] | None = None


def plain(value: Any) -> Any:
    """`scval.to_native` output made JSON-plain: addresses as strkeys, bytes as hex."""
    if isinstance(value, Address):
        return value.address
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(plain(k)): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def decode_scval(val: stellar_xdr.SCVal) -> Any:
    return plain(scval.to_native(val))


def _event_from_xdr(event: stellar_xdr.ContractEvent) -> ChainEvent | None:
    body = event.body.v0
    if body is None or event.contract_id is None:
        return None
    return ChainEvent(
        contract_id=StrKey.encode_contract(event.contract_id.contract_id.hash),
        topics=[decode_scval(t) for t in body.topics],
        value=decode_scval(body.data),
    )


def events_from_rpc(result: dict[str, Any]) -> list[ChainEvent]:
    """The contract events of a `getTransaction` result.

    Current RPC serves them decoded per operation under
    `events.contractEventsXdr`; older RPC only in `resultMetaXdr` (meta v3's
    `soroban_meta.events`, meta v4's per-operation `events`). Both are read.
    """
    out: list[ChainEvent] = []
    grouped = (result.get("events") or {}).get("contractEventsXdr")
    if isinstance(grouped, list):
        for per_op in grouped:
            for raw in per_op or []:
                ev = _event_from_xdr(stellar_xdr.ContractEvent.from_xdr(raw))
                if ev is not None:
                    out.append(ev)
        return out
    meta_b64 = result.get("resultMetaXdr")
    if not meta_b64:
        return out
    meta = stellar_xdr.TransactionMeta.from_xdr(meta_b64)
    raw_events: list[stellar_xdr.ContractEvent] = []
    if meta.v3 is not None and meta.v3.soroban_meta is not None:
        raw_events.extend(meta.v3.soroban_meta.events)
    if meta.v4 is not None:
        for op in meta.v4.operations:
            raw_events.extend(op.events)
    for raw_event in raw_events:
        ev = _event_from_xdr(raw_event)
        if ev is not None:
            out.append(ev)
    return out


def invocations_from_envelope(envelope_b64: str) -> list[Invocation]:
    """Every `invoke_contract` host function in the envelope, with plain arguments."""
    env = stellar_xdr.TransactionEnvelope.from_xdr(envelope_b64)
    if env.fee_bump is not None:
        inner = env.fee_bump.tx.inner_tx.v1
        operations = inner.tx.operations if inner is not None else []
    elif env.v1 is not None:
        operations = env.v1.tx.operations
    else:
        operations = []  # a v0 envelope predates Soroban and invokes nothing
    out: list[Invocation] = []
    for op in operations:
        invoke_op = op.body.invoke_host_function_op
        if invoke_op is None or invoke_op.host_function.invoke_contract is None:
            continue
        call = invoke_op.host_function.invoke_contract
        out.append(
            Invocation(
                contract_id=Address.from_xdr_sc_address(call.contract_address).address,
                function=call.function_name.sc_symbol.decode(),
                args=[decode_scval(a) for a in call.args],
            )
        )
    return out


@dataclass
class ChainReader:
    client: httpx.Client
    rpc_url: str
    horizon_url: str
    retry: RetryPolicy
    _ids: list[int] = field(default_factory=lambda: [0])

    # ── plumbing ────────────────────────────────────────────────
    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._ids[0] += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._ids[0], "method": method}
        if params is not None:
            payload["params"] = params

        def once() -> Any:
            response = self.client.post(self.rpc_url, json=payload, timeout=30.0)
            if response.status_code in RETRYABLE_STATUS:
                raise RetryableStatus(response.status_code, retry_after_seconds(response))
            response.raise_for_status()
            body = response.json()
            if "error" in body:
                raise RpcError(f"{method}: {body['error']}")
            return body["result"]

        return self.retry.run(once)

    def _horizon(self, path: str) -> dict[str, Any] | None:
        """GET a Horizon resource; None on 404, which is an answer."""

        def once() -> dict[str, Any] | None:
            response = self.client.get(f"{self.horizon_url}{path}", timeout=30.0)
            if response.status_code == 404:
                return None
            if response.status_code in RETRYABLE_STATUS:
                raise RetryableStatus(response.status_code, retry_after_seconds(response))
            response.raise_for_status()
            body = response.json()
            return body if isinstance(body, dict) else None

        return self.retry.run(once)

    # ── network ─────────────────────────────────────────────────
    def rpc_passphrase(self) -> str:
        return str(self._rpc("getNetwork")["passphrase"])

    def horizon_passphrase(self) -> str | None:
        root = self._horizon("/")
        return None if root is None else str(root.get("network_passphrase"))

    # ── accounts ────────────────────────────────────────────────
    def account_exists(self, account: str) -> bool:
        return self._horizon(f"/accounts/{account}") is not None

    # ── transactions ────────────────────────────────────────────
    def transaction(self, tx_hash: str) -> TxRecord:
        """RPC first (status, envelope and events); Horizon for a hash past the RPC's window."""
        result = self._rpc("getTransaction", {"hash": tx_hash})
        status = str(result.get("status") or "NOT_FOUND")
        if status in ("SUCCESS", "FAILED"):
            ledger = result.get("ledger")
            envelope = result.get("envelopeXdr")
            return TxRecord(
                tx_hash=tx_hash,
                status=status,
                ledger=int(ledger) if ledger else None,
                source="rpc",
                invocations=invocations_from_envelope(envelope) if envelope else [],
                events=events_from_rpc(result),
            )
        record = self._horizon(f"/transactions/{tx_hash}")
        if record is None:
            return TxRecord(tx_hash, "NOT_FOUND", None, "none")
        ledger = record.get("ledger")
        envelope = record.get("envelope_xdr")
        return TxRecord(
            tx_hash=tx_hash,
            status="SUCCESS" if record.get("successful") is True else "FAILED",
            ledger=int(ledger) if ledger else None,
            source="horizon",
            invocations=invocations_from_envelope(envelope) if envelope else [],
            events=None,
        )

    # ── contract views ──────────────────────────────────────────
    def simulate(self, contract_id: str, function: str, args: list[stellar_xdr.SCVal]) -> Any:
        """Run a contract VIEW in simulation and return its plain value.

        The envelope is built locally, never signed and never sent.
        """
        tx = (
            TransactionBuilder(Account(SIMULATION_SOURCE, 0), network_passphrase=TESTNET_PASSPHRASE, base_fee=100)
            .append_invoke_contract_function_op(contract_id=contract_id, function_name=function, parameters=args)
            .set_timeout(30)
            .build()
        )
        result = self._rpc("simulateTransaction", {"transaction": tx.to_xdr()})
        if result.get("error"):
            # The first line names the failure; the rest is kilobytes of event log.
            raise SimulationError(str(result["error"]).strip().splitlines()[0][:300])
        results = result.get("results") or []
        if not results or not results[0].get("xdr"):
            raise SimulationError(f"{function}: simulation returned no value")
        return decode_scval(stellar_xdr.SCVal.from_xdr(results[0]["xdr"]))

    def owner_of(self, registry_id: str, agent_id: str) -> Any:
        """`AgentRegistry.owner_of(agent_id)`; raises `SimulationError` for an id it does not hold."""
        return self.simulate(registry_id, "owner_of", [scval.to_symbol(agent_id)])

    def registry_record(self, registry_id: str, agent_id: str) -> dict[str, Any] | None:
        """`AgentRegistry.get(agent_id)` — the record, whose `active` the claim is checked against."""
        value = self.simulate(registry_id, "get", [scval.to_symbol(agent_id)])
        return value if isinstance(value, dict) else None

    def authorization(self, escrow_id: str, auth_id_hex: str) -> dict[str, Any] | None:
        """`PaymentEscrow.authorization(auth_id)`, or None when the escrow no longer holds it."""
        try:
            value = self.simulate(escrow_id, "authorization", [scval.to_bytes(bytes.fromhex(auth_id_hex))])
        except (SimulationError, ValueError):
            return None
        return value if isinstance(value, dict) else None
