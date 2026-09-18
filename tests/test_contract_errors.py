"""A contract's own rejection, carried as a code rather than as text.

When ReputationLedger refuses a rating it answers with a value of its `Error`
enum — Unauthorized, Replay, OutOfRange, NotFound — and the simulation error
that reports it opens with "HostError: Error(Contract, #N)" before running on
into the full diagnostic event log. The code is what a caller needs; the text
is what it must never repeat. Nothing here touches the network.
"""

from __future__ import annotations

from typing import Any

import pytest
from stellar_sdk import Account, Keypair, StrKey
from stellar_sdk.exceptions import PrepareTransactionException
from stellar_sdk.soroban_rpc import SimulateTransactionResponse

from app.stellar import client as sc

# Verbatim head of a real testnet simulation of ReputationLedger.submit from a
# caller that is not the Scorer (public chain data, truncated in the middle of
# the event log exactly as far as a caller would ever need).
UNAUTHORIZED_SIMULATION_ERROR = (
    "HostError: Error(Contract, #1)\n\nEvent log (newest first):\n   0: [Diagnostic Event] "
    "contract:CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT, topics:[error, Error(Contract, #1)], "
    'data:"escalating Ok(ScErrorType::Contract) frame-exit to Err"\n   1: [Diagnostic Event] '
    "topics:[fn_call, CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT, submit], data:[GD6K…]"
)


# ── reading the code off a simulation error ─────────────────────


def test_the_live_unauthorized_rejection_parses_to_its_code():
    assert sc._contract_error_code(UNAUTHORIZED_SIMULATION_ERROR) == 1


def test_every_ledger_error_code_parses():
    for code in (1, 2, 7, 100):
        assert sc._contract_error_code(f"HostError: Error(Contract, #{code})\n\nEvent log") == code


def test_a_host_error_that_is_not_the_contracts_has_no_code():
    """A budget overrun or a trap is the host's failure, not a contract
    verdict, and must not be named as one."""
    assert sc._contract_error_code("HostError: Error(Budget, ExceededLimit)\n\nEvent log") is None
    assert sc._contract_error_code("HostError: Error(WasmVm, InvalidAction)") is None


def test_only_the_head_names_the_failure():
    """The event log beneath the head can quote other contract errors; the
    call failed with the one at the top, so a code quoted further down is
    not evidence of anything."""
    assert sc._contract_error_code("transaction simulation failed; see Error(Contract, #7) in log") is None


def test_no_error_text_has_no_code():
    assert sc._contract_error_code(None) is None
    assert sc._contract_error_code("") is None


# ── the backend-signed path raises it as data ───────────────────

LEDGER_ID = StrKey.encode_contract(b"\x07" * 32)


class _RejectingServer:
    """Loads the signer's account, then fails simulation with `error`."""

    def __init__(self, error: str) -> None:
        self.error = error
        self.sent = False

    def load_account(self, account_id: str) -> Account:
        return Account(account_id, 1)

    def prepare_transaction(self, tx: Any) -> Any:
        response = SimulateTransactionResponse.model_validate({"error": self.error, "latestLedger": 1})
        raise PrepareTransactionException("simulation failed", response)

    def send_transaction(self, tx: Any) -> Any:  # pragma: no cover - must never run
        self.sent = True
        raise AssertionError("a rejected simulation was sent")


def _reject_with(monkeypatch, error: str) -> _RejectingServer:
    server = _RejectingServer(error)
    monkeypatch.setattr(sc, "_signer_keypair", lambda: Keypair.from_raw_ed25519_seed(b"\x03" * 32))
    monkeypatch.setattr(sc, "_server", lambda *, submit=False: server)
    return server


def test_a_contract_rejection_raises_a_contract_error_with_its_code(monkeypatch):
    server = _reject_with(monkeypatch, UNAUTHORIZED_SIMULATION_ERROR)
    with pytest.raises(sc.ContractError) as caught:
        sc._send_server_signed(LEDGER_ID, "submit", [])
    assert caught.value.code == 1
    assert not server.sent


def test_a_contract_error_is_still_a_runtime_error(monkeypatch):
    """Charge and seal callers catch RuntimeError; a narrower type must not
    slip past them."""
    _reject_with(monkeypatch, "HostError: Error(Contract, #7)\n\nEvent log")
    with pytest.raises(RuntimeError, match="prepare failed"):
        sc._send_server_signed(LEDGER_ID, "submit", [])


def test_any_other_simulation_failure_stays_a_plain_runtime_error(monkeypatch):
    _reject_with(monkeypatch, "HostError: Error(Budget, ExceededLimit)")
    with pytest.raises(RuntimeError) as caught:
        sc._send_server_signed(LEDGER_ID, "submit", [])
    assert not isinstance(caught.value, sc.ContractError)
