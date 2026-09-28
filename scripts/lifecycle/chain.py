"""Read-only Soroban RPC and Horizon: the ledger's own answer to every claim.

Nothing here builds a transaction that is ever sent. Contract views are read
by `simulateTransaction`, which executes nothing on the ledger, and a
transaction's fate is read by `getTransaction` (Soroban RPC) and, once it is
older than the RPC's retention window, by Horizon's `/transactions/{hash}`.

Every on-chain status the evidence file records comes from `observe`, so a row
never says SUCCESS because the API said so — the API's word is what is being
checked.

All traffic goes through one injected `httpx.Client`, so the hermetic suite
answers it with a fake RPC and a fake Horizon (`fakes.py`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from stellar_sdk import Account, Address, TransactionBuilder, scval
from stellar_sdk import xdr as stellar_xdr

from .retry import RETRYABLE_STATUS, RetryableStatus, RetryPolicy, retry_after_seconds

# How many getEvents pages an event scan may read before it gives up. Each page
# is up to `_EVENT_PAGE` events of ONE contract from the transaction's ledger
# on, so the events of the transaction being verified are on the first page in
# every realistic case; the bound only stops a pathological scan.
_EVENT_PAGES = 10
_EVENT_PAGE = 200


class RpcError(Exception):
    """The RPC answered with a JSON-RPC error: a real answer, not a timeout."""


class SimulationError(Exception):
    """The contract call failed in simulation (for example: no such function)."""


@dataclass(frozen=True)
class Observation:
    """What the ledger says about one transaction hash, and who said it."""

    tx_hash: str
    status: str  # SUCCESS | FAILED | NOT_FOUND
    ledger: int | None
    source: str  # "rpc" | "horizon" | "none"


@dataclass(frozen=True)
class ChainEvent:
    contract_id: str
    tx_hash: str
    ledger: int
    topics: list[Any]
    value: Any
    successful: bool = True


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


def decode_scval(b64: str) -> Any:
    return plain(scval.to_native(stellar_xdr.SCVal.from_xdr(b64)))


@dataclass
class ChainReader:
    client: httpx.Client
    rpc_url: str
    horizon_url: str
    passphrase: str
    retry: RetryPolicy
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
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
    def network_passphrase(self) -> str:
        return str(self._rpc("getNetwork")["passphrase"])

    def latest_ledger(self) -> int:
        return int(self._rpc("getLatestLedger")["sequence"])

    # ── transactions ────────────────────────────────────────────
    def get_transaction(self, tx_hash: str) -> Observation:
        result = self._rpc("getTransaction", {"hash": tx_hash})
        status = str(result.get("status") or "NOT_FOUND")
        ledger = result.get("ledger")
        return Observation(tx_hash, status, int(ledger) if ledger else None, "rpc")

    def observe(self, tx_hash: str, budget: float) -> Observation:
        """The ledger's verdict on `tx_hash`, waiting up to `budget` seconds for one.

        Polls RPC `getTransaction` until SUCCESS or FAILED. A hash the RPC
        still cannot find when the budget runs out is asked of Horizon, which
        keeps history the RPC has already dropped. NOT_FOUND from both is
        reported as NOT_FOUND — never promoted to a guess.
        """
        deadline = self.clock() + budget
        delay = 1.0
        while True:
            seen = self.get_transaction(tx_hash)
            if seen.status in ("SUCCESS", "FAILED"):
                return seen
            if self.clock() + delay > deadline:
                break
            self.sleep(delay)
            delay = min(delay * 2, 8.0)
        record = self._horizon(f"/transactions/{tx_hash}")
        if record is None:
            return Observation(tx_hash, "NOT_FOUND", None, "none")
        status = "SUCCESS" if record.get("successful") is True else "FAILED"
        ledger = record.get("ledger")
        return Observation(tx_hash, status, int(ledger) if ledger else None, "horizon")

    def fee_charged(self, tx_hash: str) -> int | None:
        """What the SOURCE account paid in fees for `tx_hash`, in stroops (Horizon)."""
        record = self._horizon(f"/transactions/{tx_hash}")
        if record is None or record.get("fee_charged") is None:
            return None
        return int(record["fee_charged"])

    # ── events ──────────────────────────────────────────────────
    def events(self, contract_id: str, start_ledger: int, *, tx_hash: str | None = None) -> list[ChainEvent]:
        """`contract_id`'s events from `start_ledger` on, optionally only `tx_hash`'s."""
        out: list[ChainEvent] = []
        params: dict[str, Any] = {
            "startLedger": start_ledger,
            "filters": [{"type": "contract", "contractIds": [contract_id]}],
            "pagination": {"limit": _EVENT_PAGE},
        }
        for _ in range(_EVENT_PAGES):
            result = self._rpc("getEvents", params)
            batch = result.get("events") or []
            for raw in batch:
                if tx_hash is not None and raw.get("txHash") != tx_hash:
                    continue
                out.append(
                    ChainEvent(
                        contract_id=str(raw.get("contractId")),
                        tx_hash=str(raw.get("txHash")),
                        ledger=int(raw.get("ledger") or 0),
                        topics=[decode_scval(t) for t in raw.get("topic") or []],
                        value=decode_scval(raw["value"]) if raw.get("value") else None,
                        successful=raw.get("inSuccessfulContractCall", True) is not False,
                    )
                )
            cursor = result.get("cursor") or (batch[-1].get("pagingToken") or batch[-1].get("id") if batch else None)
            if len(batch) < _EVENT_PAGE or not cursor:
                break
            params = {
                "filters": params["filters"],
                "pagination": {"limit": _EVENT_PAGE, "cursor": cursor},
            }
        return out

    # ── contract views ──────────────────────────────────────────
    def simulate(self, contract_id: str, function: str, args: list[stellar_xdr.SCVal], source: str) -> Any:
        """Run a contract VIEW in simulation and return its plain value.

        The envelope is built locally from a placeholder sequence number and is
        never signed or sent; simulation does not consume the source account.
        """
        tx = (
            TransactionBuilder(Account(source, 0), network_passphrase=self.passphrase, base_fee=100)
            .append_invoke_contract_function_op(contract_id=contract_id, function_name=function, parameters=args)
            .set_timeout(30)
            .build()
        )
        result = self._rpc("simulateTransaction", {"transaction": tx.to_xdr()})
        if result.get("error"):
            raise SimulationError(str(result["error"])[:300])
        results = result.get("results") or []
        if not results or not results[0].get("xdr"):
            raise SimulationError(f"{function}: simulation returned no value")
        return decode_scval(results[0]["xdr"])

    def escrow_version(self, escrow_id: str, source: str) -> tuple[int, str]:
        """(version, how it was decided). v2 answers `version()` with 2; v1 has
        no such function, so a SIMULATION failure means v1. An RPC failure is
        not a simulation failure and is raised: an outage must never be read
        as "this is v1"."""
        try:
            value = self.simulate(escrow_id, "version", [], source)
        except SimulationError as exc:
            return 1, f"version() absent: {exc}"
        return int(value), "version() view"

    def sac_balance(self, sac_id: str, address: str, source: str) -> int:
        """`balance(address)` on the asset's SAC, in stroops."""
        return int(self.simulate(sac_id, "balance", [scval.to_address(address)], source))

    def attestation(self, registry_id: str, job_id_hex: str, source: str) -> dict[str, Any] | None:
        """AttestationRegistry.get(job_id), or None when nothing is sealed under it."""
        try:
            value = self.simulate(registry_id, "get", [scval.to_bytes(bytes.fromhex(job_id_hex))], source)
        except SimulationError:
            return None
        return value if isinstance(value, dict) else None

    def escrow_authorization(self, escrow_id: str, auth_id_hex: str, source: str) -> dict[str, Any] | None:
        """PaymentEscrow.authorization(auth_id) (v2 adds `settled`), or None."""
        try:
            value = self.simulate(escrow_id, "authorization", [scval.to_bytes(bytes.fromhex(auth_id_hex))], source)
        except SimulationError:
            return None
        return value if isinstance(value, dict) else None
