"""A contract's own rejection, carried as a code rather than as text.

When ReputationLedger refuses a rating it answers with a value of its `Error`
enum — Unauthorized, Replay, OutOfRange, NotFound — and the simulation error
that reports it opens with "HostError: Error(Contract, #N)" before running on
into the full diagnostic event log. The code is what a caller needs; the text
is what it must never repeat. Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from stellar_sdk import Account, Keypair, StrKey
from stellar_sdk.exceptions import PrepareTransactionException
from stellar_sdk.soroban_rpc import GetTransactionStatus, SendTransactionStatus, SimulateTransactionResponse

from app.config import settings
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


# ── before the send, or after it: NotSubmittedError (D-076) ─────
#
# Every raise on the backend-signed submit path is on one side of
# `sendTransaction` or the other. Before it, no transaction exists anywhere
# but this process, so nothing can land later: that is `NotSubmittedError`.
# After it — a send that raised, a DUPLICATE, a poll that lost track — the
# transaction may be on its way, and must never be reported as refused.

HOST_SIMULATION_ERROR = (
    "HostError: Error(Value, InvalidInput)\n\nEvent log (newest first):\n   0: [Diagnostic Event] "
    'topics:[error, Error(Value, InvalidInput)], data:"byte is not allowed in Symbol", 45'
)
PENDING_HASH = "ab" * 32


class _Sent:
    """What `send_transaction` answers: a status, a hash, an error XDR."""

    def __init__(self, status: SendTransactionStatus) -> None:
        self.status = status
        self.hash = PENDING_HASH
        self.error_result_xdr = "AAAAAAAAAGT////7AAAAAA==" if status != SendTransactionStatus.PENDING else None


class _Unconfirmed:
    status = GetTransactionStatus.NOT_FOUND


class _FakeRpc:
    """A Soroban RPC that fails at exactly one stage, and records whether the
    transaction was ever sent."""

    def __init__(self, *, fail_at: str | None = None, send_status: SendTransactionStatus | None = None) -> None:
        self.fail_at = fail_at
        self.send_status = send_status or SendTransactionStatus.PENDING
        self.sent = False
        self.prepared = False

    def load_account(self, account_id: str) -> Account:
        if self.fail_at == "load_account":
            raise ConnectionError("horizon/rpc unreachable while loading the signer")
        return Account(account_id, 1)

    def prepare_transaction(self, tx: Any) -> Any:
        self.prepared = True
        if self.fail_at == "simulate":
            response = SimulateTransactionResponse.model_validate({"error": HOST_SIMULATION_ERROR, "latestLedger": 1})
            raise PrepareTransactionException("simulation failed", response)
        if self.fail_at == "prepare_transport":
            raise ConnectionError("rpc unreachable during simulation")
        return tx

    def send_transaction(self, tx: Any) -> Any:
        self.sent = True
        if self.fail_at == "send_raises":
            raise ConnectionError("the connection dropped after the send")
        return _Sent(self.send_status)

    def get_transaction(self, tx_hash: str) -> Any:
        return _Unconfirmed()


def _rpc(monkeypatch, rpc: _FakeRpc, *, keypair: Keypair | None = None) -> _FakeRpc:
    monkeypatch.setattr(sc, "_signer_keypair", lambda: keypair or Keypair.from_raw_ed25519_seed(b"\x03" * 32))
    monkeypatch.setattr(sc, "_server", lambda *, submit=False: rpc)
    return rpc


def test_a_host_simulation_failure_is_not_submitted(monkeypatch):
    """The live D-076 refusal: the host, not the contract, rejects the call at
    simulation. Nothing was signed or sent, and the type says so."""
    rpc = _rpc(monkeypatch, _FakeRpc(fail_at="simulate"))
    with pytest.raises(sc.NotSubmittedError) as caught:
        sc._send_server_signed(LEDGER_ID, "submit", [])
    # Raised as itself, not rewrapped by the stage guard around it: the head
    # is the host's error, once, which is what an operator reads.
    assert str(caught.value) == f"prepare failed: {HOST_SIMULATION_ERROR}"
    assert rpc.prepared and not rpc.sent


def test_a_contract_rejection_is_not_submitted_either(monkeypatch):
    _reject_with(monkeypatch, UNAUTHORIZED_SIMULATION_ERROR)
    with pytest.raises(sc.NotSubmittedError):
        sc._send_server_signed(LEDGER_ID, "submit", [])


@pytest.mark.parametrize(
    ("fail_at", "stage"),
    [("load_account", "load_account failed"), ("prepare_transport", "prepare failed")],
)
def test_an_rpc_failure_before_the_send_is_not_submitted(monkeypatch, fail_at, stage):
    rpc = _rpc(monkeypatch, _FakeRpc(fail_at=fail_at))
    with pytest.raises(sc.NotSubmittedError, match=stage):
        sc._send_server_signed(LEDGER_ID, "submit", [])
    assert not rpc.sent


def test_a_build_that_fails_is_not_submitted(monkeypatch):
    """A contract id that is not one fails the build, before any RPC call
    that could carry a transaction."""
    rpc = _rpc(monkeypatch, _FakeRpc())
    with pytest.raises(sc.NotSubmittedError, match="build failed"):
        sc._send_server_signed("CFAKELEDGER", "submit", [])
    assert not rpc.prepared and not rpc.sent


def test_a_signature_that_fails_is_not_submitted(monkeypatch):
    """A keypair with no secret cannot sign: the envelope never leaves."""
    public_only = Keypair.from_public_key(Keypair.random().public_key)
    rpc = _rpc(monkeypatch, _FakeRpc(), keypair=public_only)
    with pytest.raises(sc.NotSubmittedError, match="sign failed"):
        sc._send_server_signed(LEDGER_ID, "submit", [])
    assert rpc.prepared and not rpc.sent


@pytest.mark.parametrize("key", ["", "SNOTAKEY", "abandon " * 11 + "zebra"])
def test_a_signing_key_that_will_not_parse_is_not_submitted(monkeypatch, key):
    monkeypatch.setattr(settings, "stellar_signing_key", key)
    sc._signer_keypair.cache_clear()
    try:
        with pytest.raises(sc.NotSubmittedError, match="STELLAR_SIGNING_KEY"):
            sc._signer_keypair()
    finally:
        sc._signer_keypair.cache_clear()


def test_rating_arguments_that_will_not_encode_are_not_submitted(monkeypatch):
    monkeypatch.setattr(sc, "_signer_keypair", lambda: Keypair.from_raw_ed25519_seed(b"\x03" * 32))
    with pytest.raises(sc.NotSubmittedError, match="args failed"):
        sc._submit_rating_args("agt", b"\x00" * 16, 10, 1, "not-an-address", "dispute")


@pytest.mark.parametrize("status", [SendTransactionStatus.ERROR, SendTransactionStatus.TRY_AGAIN_LATER])
def test_a_send_the_rpc_refused_is_not_submitted(monkeypatch, status):
    """ERROR and TRY_AGAIN_LATER: the RPC answered, and holds nothing."""
    _rpc(monkeypatch, _FakeRpc(send_status=status))
    with pytest.raises(sc.NotSubmittedError, match="submit failed"):
        sc._send_server_signed(LEDGER_ID, "submit", [])


def test_a_duplicate_send_may_still_land_and_is_not_called_refused(monkeypatch):
    """DUPLICATE says an identical transaction is already pending. It may land."""
    _rpc(monkeypatch, _FakeRpc(send_status=SendTransactionStatus.DUPLICATE))
    with pytest.raises(RuntimeError, match="submit failed") as caught:
        sc._send_server_signed(LEDGER_ID, "submit", [])
    assert not isinstance(caught.value, sc.NotSubmittedError)


def test_a_send_that_raises_may_still_land_and_is_not_called_refused(monkeypatch):
    """The request may have reached the RPC before the connection dropped."""
    rpc = _rpc(monkeypatch, _FakeRpc(fail_at="send_raises"))
    with pytest.raises(sc.InFlightError) as caught:
        sc._send_server_signed(LEDGER_ID, "submit", [])
    assert rpc.sent
    assert not isinstance(caught.value, sc.NotSubmittedError)
    assert isinstance(caught.value.__cause__, ConnectionError)


class _SigningRpc(_FakeRpc):
    """A `_FakeRpc` that remembers the hash of the envelope it was handed."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.signed_hash: str | None = None

    def send_transaction(self, tx: Any) -> Any:
        self.signed_hash = tx.hash_hex()
        return super().send_transaction(tx)


def test_a_send_that_raises_carries_the_hash_of_what_it_signed(monkeypatch):
    """B4: the hash is known once the envelope is signed, and it is the one
    fact a reconciliation needs — so a failure after the send must carry it."""
    rpc = _rpc(monkeypatch, _SigningRpc(fail_at="send_raises"))
    with pytest.raises(sc.InFlightError) as caught:
        sc._send_server_signed(LEDGER_ID, "submit", [])
    assert rpc.signed_hash is not None and len(rpc.signed_hash) == 64
    assert caught.value.tx_hash == rpc.signed_hash


def test_a_duplicate_send_carries_the_hash_of_what_it_signed(monkeypatch):
    rpc = _rpc(monkeypatch, _SigningRpc(send_status=SendTransactionStatus.DUPLICATE))
    with pytest.raises(sc.InFlightError, match="submit failed") as caught:
        sc._send_server_signed(LEDGER_ID, "submit", [])
    assert caught.value.tx_hash == rpc.signed_hash


class _PollRaises(_FakeRpc):
    def get_transaction(self, tx_hash: str) -> Any:
        raise ConnectionError("rpc unreachable while polling")


def test_a_poll_that_raises_after_the_send_carries_the_pending_hash(monkeypatch):
    """Sent and PENDING, then the poll itself raised: in flight, with its hash."""
    _rpc(monkeypatch, _PollRaises())
    with pytest.raises(sc.InFlightError) as caught:
        asyncio.run(sc.invoke_with_server_key_async(LEDGER_ID, "submit", []))
    assert caught.value.tx_hash == PENDING_HASH
    assert isinstance(caught.value.__cause__, ConnectionError)
    with pytest.raises(sc.InFlightError) as caught_sync:
        sc.invoke_with_server_key(LEDGER_ID, "submit", [])
    assert caught_sync.value.tx_hash == PENDING_HASH


def test_a_poll_that_runs_out_after_the_send_is_a_timeout_not_a_refusal(monkeypatch):
    """Sent and PENDING, then never confirmed: the client's `timeout`, with
    the in-flight hash — never an exception of either kind."""
    rpc = _rpc(monkeypatch, _FakeRpc())
    monkeypatch.setattr(sc, "_POLL_BUDGET_SECONDS", 0.0)
    result = asyncio.run(sc.invoke_with_server_key_async(LEDGER_ID, "submit", []))
    assert rpc.sent
    assert result == {"hash": PENDING_HASH, "status": "timeout"}


def test_not_submitted_is_still_caught_by_an_except_runtime_error(monkeypatch):
    """The subclass is the compatibility promise: a caller written against
    RuntimeError, as the charge and seal paths were, still catches it."""
    _rpc(monkeypatch, _FakeRpc(fail_at="simulate"))
    try:
        sc._send_server_signed(LEDGER_ID, "submit", [])
    except RuntimeError as caught:
        assert isinstance(caught, sc.NotSubmittedError)
    else:  # pragma: no cover - the simulation always refuses
        pytest.fail("the refused simulation raised nothing")


def test_a_user_signed_envelope_that_will_not_decode_is_not_submitted(monkeypatch):
    rpc = _rpc(monkeypatch, _FakeRpc())
    with pytest.raises(sc.NotSubmittedError, match="bad signed XDR"):
        sc._send_signed_xdr("not an envelope")
    assert not rpc.sent


def test_a_refund_refused_before_the_send_is_failed_and_releases_its_claim(monkeypatch):
    """A refusal before the send is FAILED on the refund path too.

    Refunds were first left out of D-076, on the grounds that loosening the
    rule that stops a double credit should not happen by accident. It is done
    on purpose now, for the reason the rating path gave: the type is proof that
    no transaction exists, so nothing can land later. Filing it as TIMEOUT
    parked the dispute in `crediting` over a transfer that was never sent."""
    from app.services import refund_svc
    from app.services.dispute_store import DisputeRecord

    async def _refused(buyer: str, amount_usdc: float) -> dict[str, Any]:
        raise sc.NotSubmittedError("prepare failed: HostError: Error(Value, InvalidInput)")

    monkeypatch.setattr(refund_svc, "execute_refund", _refused)
    dispute = DisputeRecord(
        id="dsp_refund",
        job_id_hex="9f" * 16,
        task_id="tsk",
        step_index=0,
        agent_id="agt",
        payer=Keypair.random().public_key,
        reason="r",
        status="crediting",
        charged_usdc=0.05,
        creditable_usdc=0.05,
        opened_at=1.0,
    )
    outcome = asyncio.run(refund_svc.credit_refund(dispute, 0.05))
    assert (outcome.status, outcome.tx_hash) == ("FAILED", None)
