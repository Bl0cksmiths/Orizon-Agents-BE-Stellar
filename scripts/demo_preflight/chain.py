"""Read-only testnet reads: Soroban RPC for contract views, Horizon for accounts.

Nothing here builds a transaction that is ever signed or sent, and nothing
needs a secret. A contract VIEW (`PaymentEscrow.version`, `settler`) is read by
`simulateTransaction` on an envelope built from a placeholder source at
sequence 0 — the backend's `simulate_read(load_source=False)` recipe, which
answers identically for a never-funded source — re-stated here so the tool
never imports `app` at runtime.

A simulation that FAILS is an answer ("this contract has no such function");
an RPC that does not answer is not, and is raised as an error so a caller can
never read an outage as "this is v1".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from stellar_sdk import Account, Address, TransactionBuilder, scval
from stellar_sdk import xdr as stellar_xdr

from .config import BASE_RESERVE, STROOPS_PER_UNIT, TESTNET_PASSPHRASE
from .retry import RETRYABLE_STATUS, RetryableStatus, RetryPolicy, retry_after_seconds

# The all-zero account key: a valid strkey no one holds, used as the source of
# every simulation. Simulation never checks the source exists.
SIMULATION_SOURCE = "GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF"

# What a read can fail with, as opposed to answering.
READ_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, RetryableStatus, ValueError, KeyError)


class RpcError(Exception):
    """The RPC answered with a JSON-RPC error: a real answer, not a timeout."""


class SimulationError(Exception):
    """The contract call failed in simulation (no such function, a trap)."""


@dataclass(frozen=True)
class AccountFacts:
    """One account as Horizon reports it, in whole units (XLM on testnet)."""

    account: str
    native: float
    selling_liabilities: float
    subentries: int
    sponsoring: int
    sponsored: int

    @property
    def minimum_balance(self) -> float:
        """Stellar's reserve: (2 + subentries + sponsoring - sponsored) base reserves."""
        return (2 + self.subentries + self.sponsoring - self.sponsored) * BASE_RESERVE

    @property
    def spendable(self) -> float:
        return self.native - self.minimum_balance - self.selling_liabilities


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


def _units(raw: Any) -> float:
    """A Horizon amount string ("12.3456789") in whole units, exactly to the stroop."""
    text = str(raw or "0")
    whole, _, frac = text.partition(".")
    stroops = int(whole or "0") * STROOPS_PER_UNIT + int((frac + "0000000")[:7])
    return stroops / STROOPS_PER_UNIT


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
    def account(self, account: str) -> AccountFacts | None:
        """The account's native balance and reserve inputs; None when it does not exist."""
        record = self._horizon(f"/accounts/{account}")
        if record is None:
            return None
        native = next((b for b in record.get("balances") or [] if b.get("asset_type") == "native"), {})
        return AccountFacts(
            account=account,
            native=_units(native.get("balance")),
            selling_liabilities=_units(native.get("selling_liabilities")),
            subentries=int(record.get("subentry_count") or 0),
            sponsoring=int(record.get("num_sponsoring") or 0),
            sponsored=int(record.get("num_sponsored") or 0),
        )

    # ── contract views ──────────────────────────────────────────
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
        return plain(scval.to_native(stellar_xdr.SCVal.from_xdr(results[0]["xdr"])))

    def escrow_version(self, escrow_id: str) -> int:
        """2 from v2's `version()`. v1 has no such function, so a SIMULATION failure is v1;
        an RPC failure is raised, never read as v1."""
        try:
            return int(self.simulate(escrow_id, "version"))
        except SimulationError:
            return 1

    def escrow_settler(self, escrow_id: str) -> str:
        return str(self.simulate(escrow_id, "settler"))
