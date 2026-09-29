"""Re-verifying a transaction hash, read-only: Soroban RPC first, Horizon second.

`getTransaction` answers SUCCESS or FAILED for a hash inside the RPC's
retention window (about seven days) and NOT_FOUND for anything older, so a
NOT_FOUND — or an RPC that does not answer — is asked of Horizon, which keeps
the whole history. A hash neither knows is NOT_FOUND; one that neither could be
ASKED about is UNREADABLE, which is a different fact and never promoted to a
verdict either way.

Nothing is built, signed or sent; no secret is held.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .retry import RETRYABLE_STATUS, RetryableStatus, RetryPolicy, retry_after_seconds

SUCCESS = "SUCCESS"
FAILED = "FAILED"
NOT_FOUND = "NOT_FOUND"
UNREADABLE = "UNREADABLE"
MALFORMED = "MALFORMED"


class RpcError(Exception):
    """The RPC answered with a JSON-RPC error."""


READ_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, RetryableStatus, RpcError, ValueError, KeyError)


@dataclass(frozen=True)
class Verdict:
    tx_hash: str
    status: str  # SUCCESS | FAILED | NOT_FOUND | UNREADABLE | MALFORMED
    ledger: int | None
    source: str  # "rpc" | "horizon" | "none"
    note: str = ""

    @property
    def verified(self) -> bool:
        return self.status == SUCCESS


@dataclass
class ChainReader:
    client: httpx.Client
    rpc_url: str
    horizon_url: str
    retry: RetryPolicy
    _ids: list[int] = field(default_factory=lambda: [0])

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

    def rpc_passphrase(self) -> str:
        return str(self._rpc("getNetwork")["passphrase"])

    def horizon_passphrase(self) -> str | None:
        root = self._horizon("/")
        return None if root is None else str(root.get("network_passphrase"))

    def verify(self, tx_hash: str) -> Verdict:
        rpc_note = ""
        try:
            result = self._rpc("getTransaction", {"hash": tx_hash})
            status = str(result.get("status") or NOT_FOUND)
            if status in (SUCCESS, FAILED):
                ledger = result.get("ledger")
                return Verdict(tx_hash, status, int(ledger) if ledger else None, "rpc")
        except READ_ERRORS as exc:
            rpc_note = f"RPC unreadable ({type(exc).__name__}); "
        try:
            record = self._horizon(f"/transactions/{tx_hash}")
        except READ_ERRORS as exc:
            return Verdict(tx_hash, UNREADABLE, None, "none", f"{rpc_note}Horizon unreadable ({type(exc).__name__})")
        if record is None:
            if rpc_note:
                # Horizon's 404 is an answer, but the RPC never gave one: the
                # hash may be too new for Horizon's ingestion. Not a verdict.
                return Verdict(tx_hash, UNREADABLE, None, "none", f"{rpc_note}Horizon has no record yet")
            return Verdict(tx_hash, NOT_FOUND, None, "none", "neither the RPC nor Horizon knows this hash")
        ledger = record.get("ledger")
        status = SUCCESS if record.get("successful") is True else FAILED
        return Verdict(tx_hash, status, int(ledger) if ledger else None, "horizon", rpc_note.rstrip("; "))
