"""Read-only testnet reads: Soroban RPC for contract state and views, Horizon for account histories.

Nothing here builds a transaction that is ever signed or sent, and nothing
needs a secret. A contract VIEW (`AgentRegistry.list_ids`, `PaymentEscrow.receipt`,
`ReputationLedger.rep_state`) is read by `simulateTransaction` on an envelope
built from a placeholder source at sequence 0 — the backend's
`simulate_read(load_source=False)` recipe — and a contract's instance storage
(its roles, and the escrow's id `Nonce`) by `getLedgerEntries`. Both are
re-stated here so the tool never imports `app` at runtime.

Why state and not events: Soroban RPC keeps about seven days of events, so a
count read from `getEvents` would silently shrink as the sprint ages. The
escrow's `Nonce` numbers every authorization and receipt it has ever issued,
and each is still readable by id; the ledger's `rep_state` carries lifetime
counts. Transaction hashes for the proof links come from Horizon's operation
history of the accounts involved, which keeps every operation.

A simulation that FAILS is an answer ("no such receipt", "no such
function"); an RPC or Horizon that does not answer is not, and is raised so a
caller can never read an outage as "none".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from stellar_sdk import Account, Address, TransactionBuilder, scval
from stellar_sdk import xdr as stellar_xdr

from .config import TESTNET_PASSPHRASE
from .retry import RETRYABLE_STATUS, RetryableStatus, RetryPolicy, retry_after_seconds

# The all-zero account key: a valid strkey no one holds, used as the source of
# every simulation. Simulation never checks the source exists.
SIMULATION_SOURCE = "GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF"

# Horizon pages of operations read per account before the history counts as
# unreadable. 200 per page: far past any account this sprint has touched.
MAX_OPERATION_PAGES = 50
OPERATIONS_PAGE = 200


class RpcError(Exception):
    """The RPC answered with a JSON-RPC error: a real answer, not a timeout."""


class SimulationError(Exception):
    """The contract call failed in simulation (no such entry, no such function)."""


class HistoryTooLong(Exception):
    """An account's operation history runs past `MAX_OPERATION_PAGES`."""


# What a read can fail with, as opposed to answering.
READ_ERRORS: tuple[type[Exception], ...] = (
    httpx.HTTPError,
    RetryableStatus,
    RpcError,
    HistoryTooLong,
    ValueError,
    KeyError,
    TypeError,
)


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


def decode_scval(raw_b64: str) -> Any:
    return plain(scval.to_native(stellar_xdr.SCVal.from_xdr(raw_b64)))


def _decode_or_none(raw_b64: str) -> Any:
    """One invocation argument, or None when it will not decode (a muxed address on an older SDK)."""
    try:
        return decode_scval(raw_b64)
    except Exception:  # one odd argument must not hide the rest of the history
        return None


@dataclass(frozen=True)
class BalanceChange:
    """One asset movement Horizon attributes to an operation (`asset_balance_changes`)."""

    kind: str  # transfer | mint | burn | clawback
    source: str | None
    destination: str | None
    amount_stroops: int
    destination_muxed_id: str | None = None


@dataclass(frozen=True)
class Operation:
    """One successful operation from an account's Horizon history."""

    tx_hash: str
    created_at: str  # ISO 8601, UTC
    source_account: str
    type: str
    contract_id: str | None = None  # invoke_host_function only
    function: str | None = None
    args: list[Any] = field(default_factory=list)
    balance_changes: list[BalanceChange] = field(default_factory=list)

    @property
    def date(self) -> str:
        return self.created_at[:10]


def units_to_stroops(raw: Any) -> int:
    """A Horizon amount string ("12.3456789") in stroops, exactly."""
    text = str(raw or "0")
    negative = text.startswith("-")
    whole, _, frac = text.lstrip("-").partition(".")
    stroops = int(whole or "0") * 10_000_000 + int((frac + "0000000")[:7])
    return -stroops if negative else stroops


def operation_from_horizon(record: dict[str, Any]) -> Operation:
    changes = [
        BalanceChange(
            kind=str(c.get("type") or ""),
            source=c.get("from"),
            destination=c.get("to"),
            amount_stroops=units_to_stroops(c.get("amount")),
            destination_muxed_id=c.get("destination_muxed_id"),
        )
        for c in record.get("asset_balance_changes") or []
        if isinstance(c, dict)
    ]
    contract_id: str | None = None
    function: str | None = None
    args: list[Any] = []
    if record.get("type") == "invoke_host_function":
        params = [p.get("value") for p in record.get("parameters") or [] if isinstance(p, dict)]
        decoded = [_decode_or_none(p) for p in params if isinstance(p, str)]
        if len(decoded) >= 2 and isinstance(decoded[0], str) and isinstance(decoded[1], str):
            contract_id, function, args = decoded[0], decoded[1], decoded[2:]
    return Operation(
        tx_hash=str(record["transaction_hash"]),
        created_at=str(record["created_at"]),
        source_account=str(record.get("source_account") or ""),
        type=str(record.get("type") or ""),
        contract_id=contract_id,
        function=function,
        args=args,
        balance_changes=changes,
    )


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

    # ── contract state ──────────────────────────────────────────
    def simulate(self, contract_id: str, function: str, args: list[stellar_xdr.SCVal] | None = None) -> Any:
        """Run a contract VIEW in simulation and return its plain value. Built locally, never signed or sent."""
        tx = (
            TransactionBuilder(Account(SIMULATION_SOURCE, 0), network_passphrase=TESTNET_PASSPHRASE, base_fee=100)
            .append_invoke_contract_function_op(contract_id=contract_id, function_name=function, parameters=args or [])
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
        return decode_scval(results[0]["xdr"])

    def instance_storage(self, contract_id: str) -> dict[str, Any]:
        """The contract instance's storage, keyed by the `DataKey` variant name (`Admin`, `Nonce`, ...).

        Only unit variants are instance storage in these contracts; each is
        stored under a one-element vector holding its name.
        """
        key = stellar_xdr.LedgerKey(
            type=stellar_xdr.LedgerEntryType.CONTRACT_DATA,
            contract_data=stellar_xdr.LedgerKeyContractData(
                contract=Address(contract_id).to_xdr_sc_address(),
                key=stellar_xdr.SCVal(stellar_xdr.SCValType.SCV_LEDGER_KEY_CONTRACT_INSTANCE),
                durability=stellar_xdr.ContractDataDurability.PERSISTENT,
            ),
        )
        result = self._rpc("getLedgerEntries", {"keys": [key.to_xdr()]})
        entries = result.get("entries") or []
        if not entries:
            raise SimulationError(f"{contract_id} has no contract instance on this network")
        data = stellar_xdr.LedgerEntryData.from_xdr(entries[0]["xdr"])
        instance = data.contract_data.val.instance if data.contract_data is not None else None
        storage = instance.storage.sc_map if instance is not None and instance.storage is not None else []
        out: dict[str, Any] = {}
        for item in storage:
            k = plain(scval.to_native(item.key))
            name = k[0] if isinstance(k, list) and len(k) == 1 else k
            out[str(name)] = plain(scval.to_native(item.val))
        return out

    def escrow_version(self, escrow_id: str) -> int:
        """2 from v2's `version()`. v1 has no such function, so a SIMULATION failure is v1;
        an RPC failure is raised, never read as v1."""
        try:
            return int(self.simulate(escrow_id, "version"))
        except SimulationError:
            return 1

    # ── account histories ───────────────────────────────────────
    def operations(self, account: str) -> list[Operation]:
        """Every successful operation this account is party to, oldest first. [] for an account that
        does not exist (Horizon's 404 is an answer)."""
        out: list[Operation] = []
        cursor = ""
        for _ in range(MAX_OPERATION_PAGES):
            path = f"/accounts/{account}/operations?order=asc&limit={OPERATIONS_PAGE}"
            if cursor:
                path += f"&cursor={cursor}"
            page = self._horizon(path)
            if page is None:
                return out
            records = [r for r in (page.get("_embedded") or {}).get("records") or [] if isinstance(r, dict)]
            out.extend(operation_from_horizon(r) for r in records)
            if len(records) < OPERATIONS_PAGE:
                return out
            cursor = str(records[-1]["paging_token"])
        raise HistoryTooLong(f"{account} has more than {MAX_OPERATION_PAGES * OPERATIONS_PAGE} operations")


def id_bytes(n: int) -> stellar_xdr.SCVal:
    """The escrow's n-th id: `BytesN<16>`, the nonce big-endian in the low eight bytes."""
    return scval.to_bytes(n.to_bytes(16, "big"))


def id_hex(n: int) -> str:
    return n.to_bytes(16, "big").hex()
