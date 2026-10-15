"""Every `reconcile_claim` decision pinned whole: action, hash, chain word and detail.

The store, the RPC lookup, the transfer identification and the two writers are
replaced with recorders, so each branch of the decision is driven exactly and
what it hands to which writer is asserted. The end-to-end behaviour against a
real store and real envelopes is `test_refund_reconcile.py`'s; this file pins
the decision table itself. Nothing reaches a network.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from test_dispute_store import a_dispute

import app.stellar.client as sc
from app.services import refund_reconcile
from app.services.dispute_store import DisputeRecord, RefundClaim
from app.services.refund_reconcile import (
    EXPIRY_MARGIN_SECONDS,
    HISTORY_MARGIN_SECONDS,
    MIN_CLAIM_AGE_SECONDS,
    REFUND_TX_TIMEOUT_SECONDS,
    Decision,
    _Transfer,
    reconcile_claim,
)

CLAIMED_AT = 1_700_000_000.0
NOW = CLAIMED_AT + 3_600
AGE = NOW - CLAIMED_AT
UPDATED_AT = CLAIMED_AT + 10
TX = "tx_in_flight"
SETTLER = "GSETTLER"
SAC = "CSAC"


class Store:
    """`get_dispute` and `list_refund_claims`, answered from what the test sets."""

    def __init__(self, dispute: DisputeRecord | None, claims: tuple[RefundClaim, ...] = ()) -> None:
        self.dispute = dispute
        self.claims = claims
        self.calls: list[str] = []

    async def get_dispute(self, dispute_id: str) -> DisputeRecord | None:
        self.calls.append(f"get_dispute {dispute_id}")
        return self.dispute

    async def list_refund_claims(self) -> tuple[RefundClaim, ...]:
        self.calls.append("list_refund_claims")
        return self.claims


class World:
    """The seams `reconcile_claim` reaches, each recording what it was handed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, dispute: DisputeRecord | None, **store: Any) -> None:
        self.store = Store(dispute, **store)
        self.answer: sc.LedgerTransaction | Exception | None = None
        self.transfer: tuple[_Transfer | None, str] = (None, "never identified")
        self.calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(refund_reconcile, "get_dispute_store", lambda: self.store)
        monkeypatch.setattr(refund_reconcile, "_lookup", self._lookup)
        monkeypatch.setattr(refund_reconcile, "_identify", self._identify)
        monkeypatch.setattr(refund_reconcile, "_record_landed", self._record_landed)
        monkeypatch.setattr(refund_reconcile, "_release", self._release)

    async def _lookup(self, tx_hash: str) -> sc.LedgerTransaction:
        self.calls.append(("lookup", tx_hash))
        if isinstance(self.answer, Exception):
            raise self.answer
        assert self.answer is not None
        return self.answer

    def _identify(
        self, dispute: DisputeRecord, found: sc.LedgerTransaction, *, settler: str, asset_sac: str
    ) -> tuple[_Transfer | None, str]:
        self.calls.append(("identify", dispute.id, found.tx_hash, settler, asset_sac))
        return self.transfer

    async def _record_landed(
        self, dispute: DisputeRecord, tx_hash: str, stroops: int, base: dict[str, Any]
    ) -> Decision:
        self.calls.append(("record_landed", dispute.id, tx_hash, stroops, base))
        return Decision(dispute.id, "credited", base["claim_age_s"])

    async def _release(
        self, dispute: DisputeRecord, tx_hash: str, action: str, base: dict[str, Any], why: str
    ) -> Decision:
        self.calls.append(("release", dispute.id, tx_hash, action, base, why))
        return Decision(dispute.id, "released", base["claim_age_s"])

    def decide(self, *, now: float = NOW) -> Decision:
        claim = RefundClaim(dispute_id="dsp_0001", claimed_at=CLAIMED_AT)
        return asyncio.run(reconcile_claim(claim, now=now, settler=SETTLER, asset_sac=SAC))


def crediting(**overrides: Any) -> DisputeRecord:
    fields: dict[str, Any] = {"status": "crediting", "refund_tx": TX, "inflight_usdc": 0.05, "updated_at": UPDATED_AT}
    return dataclasses.replace(a_dispute(), **{**fields, **overrides})


def answer(status: str, **window: Any) -> sc.LedgerTransaction:
    return sc.LedgerTransaction(
        tx_hash=TX,
        status=status,
        latest_ledger=window.get("latest_ledger", 5_000_000),
        latest_ledger_close_time=window.get("latest_ledger_close_time", int(NOW)),
        oldest_ledger=window.get("oldest_ledger", 4_000_000),
        oldest_ledger_close_time=window.get("oldest_ledger_close_time", int(CLAIMED_AT - 86_400)),
        ledger=None,
        envelope_xdr=None,
    )


def base(status: str) -> dict[str, Any]:
    return {"dispute_id": "dsp_0001", "claim_age_s": AGE, "tx_hash": TX, "chain": status}


# ── before the chain is asked ────────────────────────────────────────────


def test_a_young_claim_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, crediting())
    age = MIN_CLAIM_AGE_SECONDS - 0.5
    assert world.decide(now=CLAIMED_AT + age) == Decision("dsp_0001", "young", age)
    assert world.store.calls == []
    assert world.calls == []


def test_a_claim_exactly_at_the_minimum_age_is_decided(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, None)
    assert world.decide(now=CLAIMED_AT + MIN_CLAIM_AGE_SECONDS).action == "missing"


def test_a_claim_over_an_unreadable_dispute_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, None)
    assert world.decide() == Decision(
        "dsp_0001", "missing", AGE, detail="a refund claim over a dispute that cannot be read"
    )
    assert world.store.calls == ["get_dispute dsp_0001"]
    assert world.calls == []


def test_a_claim_gone_from_the_queue_was_settled_while_the_pass_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, crediting(status="credited"), claims=(RefundClaim("dsp_other", CLAIMED_AT),))
    assert world.decide() == Decision(
        "dsp_0001", "already_settled", AGE, TX, detail="the claim was settled while this pass ran (now credited)"
    )
    assert world.store.calls == ["get_dispute dsp_0001", "list_refund_claims"] and world.calls == []


def test_a_claim_still_held_over_a_dispute_not_crediting_is_a_wedge(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(
        monkeypatch, crediting(status="upheld", refund_tx=None), claims=(RefundClaim("dsp_0001", CLAIMED_AT),)
    )
    assert world.decide() == Decision(
        "dsp_0001", "not_crediting", AGE, None, detail="the claim is held over a dispute in upheld"
    )
    assert world.calls == []


@pytest.mark.parametrize(
    ("refund_tx", "inflight", "amount"), [(None, 0.05, "0.0500000"), ("", None, "1.5000000"), (None, 0.0, "1.5000000")]
)
def test_a_crediting_dispute_with_no_hash_names_what_to_look_for(
    monkeypatch: pytest.MonkeyPatch, refund_tx: str | None, inflight: float | None, amount: str
) -> None:
    dispute = crediting(refund_tx=refund_tx, inflight_usdc=inflight)
    world = World(monkeypatch, dispute)
    assert world.decide() == Decision(
        "dsp_0001",
        "no_hash",
        AGE,
        detail=f"no in-flight hash on record — read the settler's history for a transfer to {dispute.payer}"
        f" of about {amount} USDC (docs/disputes.md)",
    )
    assert world.calls == []


def test_a_lookup_that_raises_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, crediting())
    world.answer = ConnectionError("rpc down")
    assert world.decide() == Decision(
        "dsp_0001", "rpc_error", AGE, TX, detail="ConnectionError: rpc down — nothing was changed"
    )
    assert world.calls == [("lookup", TX)]


# ── SUCCESS and FAILED: what the transaction moved ───────────────────────


@pytest.mark.parametrize("status", ["SUCCESS", "FAILED"])
def test_a_transaction_that_is_not_this_refund_is_never_acted_on(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    world = World(monkeypatch, crediting())
    world.answer = answer(status)
    world.transfer = (None, "the RPC answered with no envelope")
    assert world.decide() == Decision(
        **base(status), action="not_this_refund", detail="the RPC answered with no envelope"
    )
    assert world.calls == [("lookup", TX), ("identify", "dsp_0001", TX, SETTLER, SAC)]


@pytest.mark.parametrize("status", ["SUCCESS", "FAILED"])
def test_a_transfer_that_disagrees_with_the_in_flight_amount_is_left_for_a_human(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    world = World(monkeypatch, crediting(inflight_usdc=0.05))
    world.answer = answer(status)
    world.transfer = (_Transfer(SAC, SETTLER, "GPAYER", 499_999), "")
    assert world.decide() == Decision(
        **base(status),
        action="amount_mismatch",
        detail="the chain's transfer is 499999 stroops, the record's in-flight amount 500000",
    )
    assert [c[0] for c in world.calls] == ["lookup", "identify"]


@pytest.mark.parametrize("inflight", [0.05, None])
def test_a_landed_transfer_is_recorded(monkeypatch: pytest.MonkeyPatch, inflight: float | None) -> None:
    world = World(monkeypatch, crediting(inflight_usdc=inflight))
    world.answer = answer("SUCCESS")
    world.transfer = (_Transfer(SAC, SETTLER, "GPAYER", 500_000), "")
    assert world.decide() == Decision("dsp_0001", "credited", AGE)
    assert world.calls[-1] == ("record_landed", "dsp_0001", TX, 500_000, base("SUCCESS"))


def test_a_failed_transfer_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, crediting())
    world.answer = answer("FAILED")
    world.transfer = (_Transfer(SAC, SETTLER, "GPAYER", 500_000), "")
    assert world.decide() == Decision("dsp_0001", "released", AGE)
    assert world.calls[-1] == (
        "release",
        "dsp_0001",
        TX,
        "released",
        base("FAILED"),
        "the ledger answered FAILED, so no funds moved",
    )


# ── anything else, and NOT_FOUND ─────────────────────────────────────────


def test_an_unknown_answer_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, crediting())
    world.answer = answer("PENDING")
    assert world.decide() == Decision(
        **base("PENDING"), action="rpc_error", detail="an answer this sweep does not know — nothing was changed"
    )
    assert world.calls == [("lookup", TX)]


@pytest.mark.parametrize("after_by", [1, 0.5])
def test_a_history_that_starts_after_the_claim_cannot_say_never(
    monkeypatch: pytest.MonkeyPatch, after_by: float
) -> None:
    world = World(monkeypatch, crediting())
    oldest = CLAIMED_AT - HISTORY_MARGIN_SECONDS + after_by
    world.answer = answer("NOT_FOUND", oldest_ledger=4_100_000, oldest_ledger_close_time=oldest)
    assert world.decide() == Decision(
        **base("NOT_FOUND"),
        action="history_gap",
        detail=f"the RPC's history starts at ledger 4100000 (closed {oldest}), after this claim was taken"
        f" ({CLAIMED_AT:.0f}) — NOT_FOUND cannot tell 'never landed' from 'forgotten'; read the payer's account"
        " by hand",
    )
    assert world.calls == [("lookup", TX)]


def test_nothing_dating_the_submission_leaves_it_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, crediting(updated_at=None))
    world.answer = answer("NOT_FOUND", oldest_ledger_close_time=CLAIMED_AT - HISTORY_MARGIN_SECONDS)
    assert world.decide() == Decision(
        **base("NOT_FOUND"), action="no_expiry_bound", detail="nothing on record dates the submission"
    )
    assert world.calls == [("lookup", TX)]


def test_a_transaction_that_may_still_land_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, crediting())
    until = UPDATED_AT + REFUND_TX_TIMEOUT_SECONDS
    closed = until + EXPIRY_MARGIN_SECONDS
    world.answer = answer("NOT_FOUND", latest_ledger_close_time=closed)
    assert world.decide() == Decision(
        **base("NOT_FOUND"),
        action="pending",
        detail=f"the ledger closed at {closed}; the transaction may land until {until:.0f}, and is left alone"
        f" until {closed:.0f}",
    )
    assert world.calls == [("lookup", TX)]


def test_a_transaction_past_its_last_moment_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    world = World(monkeypatch, crediting())
    until = UPDATED_AT + REFUND_TX_TIMEOUT_SECONDS
    closed = until + EXPIRY_MARGIN_SECONDS + 1
    world.answer = answer("NOT_FOUND", latest_ledger=5_000_123, latest_ledger_close_time=closed)
    assert world.decide() == Decision("dsp_0001", "released", AGE)
    assert world.calls[-1] == (
        "release",
        "dsp_0001",
        TX,
        "expired_released",
        base("NOT_FOUND"),
        f"not on the ledger, and ledger 5000123 closed at {closed}, past its last valid moment {until:.0f}"
        f" + {EXPIRY_MARGIN_SECONDS}s — it can never land",
    )
