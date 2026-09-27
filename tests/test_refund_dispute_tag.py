"""The dispute tag on a refund: the payer's G address muxed with an id derived
from the dispute (CAP-67), so the SAC transfer event names the dispute it pays.

Everything below the RPC is real: `credit_refund` → `execute_refund` → the
client's own builder and signer. Only the server is a fake, so what is decoded
here is the envelope the settler would sign. Nothing touches the network; the
testnet evidence (protocol 28, simulation only) is in `docs/disputes.md`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from types import SimpleNamespace
from typing import Any

import pytest
from stellar_sdk import Account, Address, Keypair, MuxedAccount, StrKey
from stellar_sdk.exceptions import PrepareTransactionException
from stellar_sdk.soroban_rpc import GetTransactionStatus, SendTransactionStatus, SimulateTransactionResponse
from stellar_sdk.xdr import SCAddressType, SCVal, TransactionEnvelope

import app.stellar.client as sc
from app.services import dispute_rating, refund_svc
from app.services.dispute_store import DisputeRecord

SETTLER = Keypair.from_raw_ed25519_seed(b"\x0e" * 32)
PAYER = Keypair.from_raw_ed25519_seed(b"\x0f" * 32).public_key
SAC = StrKey.encode_contract(b"\x05" * 32)
DISPUTE_ID = "dsp_0123456789abcdef0123456789abcdef"

# The id `refund_muxed_id` gives DISPUTE_ID, worked by hand from the formula in
# docs/disputes.md, so a change of formula or tag cannot pass unnoticed.
DISPUTE_ID_MUX = int.from_bytes(hashlib.sha256(DISPUTE_ID.encode() + b"orizon-refund:v1").digest()[:8], "big")
# The same, frozen as a number: the vector the docs quote.
DOCUMENTED_ID = "dsp_deadbeefdeadbeefdeadbeefdeadbeef"
DOCUMENTED_MUX = 0x0840978ECE59F73F

# How the host refused a muxed address on testnet, verbatim.
MUXED_REFUSED = "HostError: Error(Value, UnexpectedType)\nDebugInfo not available"


def _dispute(payer: str = PAYER, dispute_id: str = DISPUTE_ID) -> DisputeRecord:
    return DisputeRecord(
        id=dispute_id,
        job_id_hex="9f8e7d6c5b4a39281706f5e4d3c2b1a0",
        task_id="tsk_tagged",
        step_index=0,
        agent_id="agt_writer",
        payer=payer,
        reason="the draft was empty",
        status="upheld",
        charged_usdc=0.05,
        creditable_usdc=0.05,
        opened_at=1_700_000_100.0,
    )


class _Rpc:
    """A Soroban RPC that records every envelope it is asked to prepare or send.

    `refuse` maps an argument predicate to a simulation error: a prepared
    transaction whose `to` matches is refused the way the RPC refuses one.
    `send_raises` makes the send itself raise, after signing.
    """

    def __init__(self, *, refuse: dict[str, str] | None = None, send_raises: bool = False) -> None:
        self.refuse = refuse or {}
        self.send_raises = send_raises
        self.prepared: list[TransactionEnvelope] = []
        self.sent: list[TransactionEnvelope] = []

    def load_account(self, account_id: str) -> Account:
        return Account(account_id, 41)

    def prepare_transaction(self, tx: Any) -> Any:
        envelope = TransactionEnvelope.from_xdr(tx.to_xdr())
        self.prepared.append(envelope)
        kind = _to(envelope).address.type.name
        if kind in self.refuse:
            response = SimulateTransactionResponse.model_validate({"error": self.refuse[kind], "latestLedger": 1})
            raise PrepareTransactionException("simulation failed", response)
        return tx

    def send_transaction(self, tx: Any) -> Any:
        self.sent.append(TransactionEnvelope.from_xdr(tx.to_xdr()))
        if self.send_raises:
            raise ConnectionError("connection reset by peer")
        return SimpleNamespace(status=SendTransactionStatus.PENDING, hash=tx.hash_hex(), error_result_xdr=None)

    def get_transaction(self, tx_hash: str) -> Any:
        return SimpleNamespace(status=GetTransactionStatus.SUCCESS, ledger=7, result_meta_xdr=None)


def _with_rpc(monkeypatch, rpc: _Rpc) -> _Rpc:
    monkeypatch.setattr(sc, "_signer_keypair", lambda: SETTLER)
    monkeypatch.setattr(sc, "_server", lambda *, submit=False: rpc)
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(asset_sac=SAC, payment_escrow="CESCROW"))
    return rpc


def _args(envelope: TransactionEnvelope) -> list[SCVal]:
    op = envelope.v1.tx.operations[0].body.invoke_host_function_op
    return list(op.host_function.invoke_contract.args)


def _to(envelope: TransactionEnvelope) -> SCVal:
    return _args(envelope)[1]


# ── the id ──────────────────────────────────────────────────────────


def test_the_refund_id_is_the_documented_formula() -> None:
    assert refund_svc.refund_muxed_id(DISPUTE_ID) == DISPUTE_ID_MUX
    assert refund_svc.refund_muxed_id(DOCUMENTED_ID) == DOCUMENTED_MUX


def test_the_refund_id_is_its_own_domain_and_keeps_disputes_apart() -> None:
    """Its own tag, never the rating's, and distinct for every dispute id."""
    assert refund_svc.REFUND_MUX_TAG != dispute_rating.DISPUTE_ID_TAG
    ids = [f"dsp_{secrets.token_hex(16)}" for _ in range(5_000)]
    assert len({refund_svc.refund_muxed_id(i) for i in ids}) == len(ids)
    assert all(0 <= refund_svc.refund_muxed_id(i) < 2**64 for i in ids)


# ── the built transaction ───────────────────────────────────────────


def test_the_signed_refund_pays_the_payer_muxed_with_its_dispute_id(monkeypatch) -> None:
    rpc = _with_rpc(monkeypatch, _Rpc())

    outcome = asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    assert (outcome.status, outcome.amount_usdc) == ("SUCCESS", 0.05)
    assert len(rpc.sent) == 1
    source, to, amount = _args(rpc.sent[0])
    assert Address.from_xdr_sc_address(source.address).address == SETTLER.public_key
    assert to.address.type == SCAddressType.SC_ADDRESS_TYPE_MUXED_ACCOUNT
    muxed = to.address.muxed_account
    assert muxed.id.uint64 == refund_svc.refund_muxed_id(DISPUTE_ID) == DISPUTE_ID_MUX
    # The funds land in the payer's own G account: the muxed address is that
    # account's key with the id beside it, and the SAC credits the account.
    assert StrKey.encode_ed25519_public_key(muxed.ed25519.uint256) == PAYER
    m_address = Address.from_xdr_sc_address(to.address).address
    assert MuxedAccount.from_account(m_address).account_id == PAYER
    assert MuxedAccount.from_account(m_address).account_muxed_id == DISPUTE_ID_MUX
    assert amount.i128.lo.uint64 == 500_000 and amount.i128.hi.int64 == 0


# ── the fallback ────────────────────────────────────────────────────


def test_a_payer_that_cannot_be_muxed_is_paid_at_its_plain_address(monkeypatch, caplog) -> None:
    """A contract wallet (C…) has no muxed form. The tag is dropped, not the credit."""
    wallet = StrKey.encode_contract(b"\x0a" * 32)
    rpc = _with_rpc(monkeypatch, _Rpc())

    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        outcome = asyncio.run(refund_svc.credit_refund(_dispute(payer=wallet), 0.05))

    assert outcome.status == "SUCCESS"
    assert len(rpc.sent) == 1
    to = _to(rpc.sent[0])
    assert to.address.type == SCAddressType.SC_ADDRESS_TYPE_CONTRACT
    assert Address.from_xdr_sc_address(to.address).address == wallet
    assert any("cannot carry its dispute tag" in r.getMessage() for r in caplog.records)


def test_an_asset_contract_that_refuses_the_muxed_address_is_paid_plain(monkeypatch, caplog) -> None:
    """Refused in simulation, so nothing was signed: the plain transfer is the only one sent."""
    rpc = _with_rpc(monkeypatch, _Rpc(refuse={"SC_ADDRESS_TYPE_MUXED_ACCOUNT": MUXED_REFUSED}))

    with caplog.at_level(logging.WARNING, logger="app.services.refund_svc"):
        outcome = asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    assert outcome.status == "SUCCESS"
    assert [_to(e).address.type.name for e in rpc.prepared] == [
        "SC_ADDRESS_TYPE_MUXED_ACCOUNT",
        "SC_ADDRESS_TYPE_ACCOUNT",
    ]
    assert len(rpc.sent) == 1
    assert Address.from_xdr_sc_address(_to(rpc.sent[0]).address).address == PAYER
    assert any("refused the muxed address" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "error",
    [
        "HostError: Error(Contract, #10)\n\nEvent log (newest first): ...",  # the settler is short of funds
        "HostError: Error(Budget, ExceededLimit)\n\nEvent log (newest first): ...",
    ],
    ids=["contract", "host"],
)
def test_any_other_refusal_is_not_retried_at_the_plain_address(monkeypatch, error: str) -> None:
    """Only the muxed refusal earns a second attempt; anything else fails as it always did."""
    rpc = _with_rpc(monkeypatch, _Rpc(refuse={"SC_ADDRESS_TYPE_MUXED_ACCOUNT": error}))

    outcome = asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    assert (outcome.status, outcome.tx_hash) == ("FAILED", None)
    assert len(rpc.prepared) == 1
    assert rpc.sent == []


def test_a_tagged_transfer_that_may_have_been_sent_is_never_paid_again(monkeypatch) -> None:
    rpc = _with_rpc(monkeypatch, _Rpc(send_raises=True))

    outcome = asyncio.run(refund_svc.credit_refund(_dispute(), 0.05))

    assert outcome.status == "TIMEOUT"
    assert len(rpc.sent) == 1
    assert _to(rpc.sent[0]).address.type == SCAddressType.SC_ADDRESS_TYPE_MUXED_ACCOUNT
    # The hash of what was signed, so the dispute records what to look for.
    assert outcome.tx_hash is not None and len(outcome.tx_hash) == 64


def test_without_a_dispute_the_transfer_is_plain(monkeypatch) -> None:
    """The 4.01 spike script's call, with no dispute, is unchanged."""
    rpc = _with_rpc(monkeypatch, _Rpc())

    asyncio.run(refund_svc.execute_refund(PAYER, 0.05))

    assert [_to(e).address.type.name for e in rpc.sent] == ["SC_ADDRESS_TYPE_ACCOUNT"]
