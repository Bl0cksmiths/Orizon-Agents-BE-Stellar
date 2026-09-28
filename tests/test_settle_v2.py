"""PaymentEscrow v2 settlement (ADR 0010): custody at authorize, one settle
that pays each DELIVERED step its own amount, receipts into the seal, and the
v1 path left exactly as it was.

Hermetic: the chain is `_Chain`, installed at the client seam —
`sc.invoke_with_server_key_async` for every signed call and `sc.simulate_read`
for every read — so the real payout builder, the real record and the real
seal composition all run, and nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from stellar_sdk import Keypair, scval
from test_settlement_logging import SIGNING_SECRET, _use_fake_signer

from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import dispute_store, dispute_svc, execution_svc
from app.services.dispute_store import InMemoryDisputeStore, SettlementRecord
from app.state import state
from app.stellar import client as sc

ESCROW = "C" + "E" * 55
REGISTRY = "C" + "R" * 55
ATTESTATION = "C" + "T" * 55
AUTH_ID_HEX = "ab" * 16
PAYER = Keypair.random().public_key
OWNER = Keypair.random().public_key
SETTLE_TX = "5e" * 32
SEAL_TX = "5a" * 32
NOT_FOUND = RuntimeError("simulate failed: HostError: Error(Contract, #2)")


@pytest.fixture(autouse=True)
def _execute_time_recheck_passes(monkeypatch):
    """The execute-time listing/floor gate is not what these tests pin."""
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def clean_state():
    yield
    for key in [t for t in state.traces if t.startswith("tsk_v2_")]:
        state.traces.pop(key, None)
    for key in [t for t in state.tasks if t.startswith("tsk_v2_")]:
        state.tasks.pop(key, None)


class _Store(InMemoryDisputeStore):
    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[SettlementRecord] = []

    async def record_settlement(self, record: SettlementRecord) -> None:
        self.recorded.append(record)
        await super().record_settlement(record)


@pytest.fixture()
def store(monkeypatch) -> _Store:
    fresh = _Store()
    monkeypatch.setattr(dispute_store, "_store", fresh)
    return fresh


class _Chain:
    """The escrow, the registry and the attestation registry, as the client sees them.

    `settle` is a dict answered as `_finalize_invoke` shapes one, or an
    exception to raise; its default mints one receipt per payout, as raw bytes
    in a list — exactly what `scval.to_native` makes of a `Vec<BytesN<16>>`.
    """

    def __init__(
        self,
        *,
        owners: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
        settle: Any = None,
        seal: Any = None,
    ) -> None:
        self.owners = owners if owners is not None else {}
        self.auth = auth
        self.settle = settle
        self.seal = seal
        self.calls: list[tuple[str, list[Any]]] = []
        self.reads: list[tuple[str, str]] = []

    async def invoke(self, contract_id: str, function_name: str, args: list[Any]) -> dict[str, Any]:
        self.calls.append((function_name, args))
        if function_name == "settle":
            assert contract_id == ESCROW
            if isinstance(self.settle, BaseException):
                raise self.settle
            if self.settle is not None:
                return self.settle
            count = len(scval.to_native(args[3]))
            return {"hash": SETTLE_TX, "status": "SUCCESS", "ledger": 7, "result": [_receipt(i) for i in range(count)]}
        if function_name == "seal":
            assert contract_id == ATTESTATION
            return self.seal or {"hash": SEAL_TX, "status": "SUCCESS", "ledger": 8, "result": None}
        if function_name == "charge":
            return {"hash": "c1" * 32, "status": "SUCCESS", "ledger": 7, "result": "cd" * 16}
        raise AssertionError(f"unexpected invoke: {function_name}")

    def read(self, contract_id: str, function_name: str, args: Any = None, source: Any = None, **_kw: Any) -> Any:
        self.reads.append((contract_id, function_name))
        if function_name == "owner_of":
            agent_id = scval.to_native(args[0])
            owner = self.owners.get(agent_id, OWNER)
            if isinstance(owner, BaseException):
                raise owner
            if owner is None:
                raise NOT_FOUND
            return owner
        if function_name == "authorization":
            if self.auth is None:
                raise NOT_FOUND
            return self.auth
        raise AssertionError(f"unexpected read: {function_name}")

    def named(self, function_name: str) -> list[list[Any]]:
        return [args for name, args in self.calls if name == function_name]


def _receipt(i: int) -> bytes:
    return bytes([0xA0 + i]) * 16


def _auth(**overrides: Any) -> dict[str, Any]:
    record = {
        "payer": PAYER,
        "agent_id": "pln_v2",
        "max_amount": 10_000_000,
        "spent": 0,
        "expires_at": int(time.time()) + 3_600,
        "revoked": False,
        "settled": False,
    }
    record.update(overrides)
    return record


def _install(monkeypatch, chain: _Chain, *, version: int = 2) -> _Chain:
    _use_fake_signer(monkeypatch)
    monkeypatch.setattr(
        sc,
        "contract_ids",
        lambda: sc.ContractIds(
            agent_registry=REGISTRY,
            reputation_ledger="",
            payment_escrow=ESCROW,
            attestation_registry=ATTESTATION,
            asset_sac="",
        ),
    )
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY)
    monkeypatch.setattr(sc, "_escrow_versions", {ESCROW: version})
    monkeypatch.setattr(sc, "invoke_with_server_key_async", chain.invoke)
    monkeypatch.setattr(sc, "simulate_read", chain.read)
    monkeypatch.setattr(execution_svc, "_owner_reads", {})
    return chain


def _plan(prices: tuple[float, ...], agent_ids: tuple[str, ...] | None = None) -> StoredPlan:
    ids = agent_ids or tuple(f"agt_{i}" for i in range(len(prices)))
    return StoredPlan(
        id="pln_v2",
        intent="build a thing",
        plan=Plan(
            steps=[
                PlanStep(agent_id=a, agent_name=f"w.{a}", rationale="r", est_price_usdc=p, est_eta_seconds=1.0)
                for a, p in zip(ids, prices, strict=True)
            ]
        ),
        total_usdc=sum(prices),
        total_eta=1.0,
    )


def _add_task(task_id: str) -> None:
    state.add_task(Task(id=task_id, intent="build a thing", agents=1, spent=0.0, status="running"))


class _Ok:
    name = "w.ok"

    async def run(self, intent, rationale, context=None):
        return {"summary": "did it"}


class _Hangs:
    name = "w.hangs"

    async def run(self, intent, rationale, context=None):
        await asyncio.sleep(60)


class _Boom:
    name = "w.boom"

    async def run(self, intent, rationale, context=None):
        raise RuntimeError("down")


def _workers(monkeypatch, by_agent: dict[str, Any]) -> None:
    async def _resolve(agent_id):
        return by_agent[agent_id]

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)

    async def _no_ratings(*a, **k):
        return None

    monkeypatch.setattr(execution_svc, "_submit_ratings", _no_ratings)


def _run(plan: StoredPlan, task_id: str, **kwargs: Any) -> None:
    _add_task(task_id)
    asyncio.run(execution_svc._run(plan, task_id, auth_id_hex=AUTH_ID_HEX, payer=PAYER, **kwargs))


def _payouts(args: list[Any]) -> list[dict[str, Any]]:
    return scval.to_native(args[3])


def _settlement_lines(task_id: str) -> list[Any]:
    return [line for line in state.traces[task_id] if line.settlement is not None]


# ── payouts derive from delivered steps only ────────────────────────────
def test_only_delivered_steps_are_paid_and_a_timed_out_step_is_not(monkeypatch, store):
    """5.01 AC5: a timed-out step did not deliver, so it is not in the payouts,
    and every step that did is paid its own price in stroops."""
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    monkeypatch.setattr(execution_svc, "STEP_TIMEOUT_SECONDS", 0.05)
    _workers(monkeypatch, {"agt_0": _Ok(), "agt_1": _Hangs(), "agt_2": _Ok()})

    _run(_plan((0.012, 0.05, 0.0371234)), "tsk_v2_timeout")

    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": "agt_0", "amount": 120_000}, {"agent_id": "agt_2", "amount": 371_234}]
    [record] = store.recorded[:1]
    assert [(s.step_index, s.delivered, s.paid_usdc) for s in record.steps] == [
        (0, True, 0.012),
        (1, False, 0.0),
        (2, True, 0.0371234),
    ]
    assert record.settled_usdc == pytest.approx(0.0491234)
    task = state.tasks["tsk_v2_timeout"]
    assert (task.settlement, task.charge_tx) == ("settled", SETTLE_TX)
    assert [line.settlement for line in _settlement_lines("tsk_v2_timeout")] == ["settled"]


def test_the_payouts_never_exceed_the_authorized_max(monkeypatch, store):
    """The authorization's `max_amount` is read back and the payouts are held
    under it — the last step cut to what is left, the one past it unpaid — and
    the cut is said loudly. The contract would refuse the WHOLE settle."""
    chain = _install(monkeypatch, _Chain(auth=_auth(max_amount=150_000)))

    result = asyncio.run(
        execution_svc._settle_v2(
            "tsk_v2_cap",
            time.monotonic(),
            _plan((0.01, 0.01, 0.01)),
            payer=PAYER,
            auth_id_hex=AUTH_ID_HEX,
            delivered_steps=frozenset({0, 1, 2}),
        )
    )

    assert result[0] == SETTLE_TX
    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": "agt_0", "amount": 100_000}, {"agent_id": "agt_1", "amount": 50_000}]
    assert sum(p["amount"] for p in _payouts(settle)) <= 150_000
    assert any(line.msg == "payouts cut to what the buyer authorized" for line in state.traces["tsk_v2_cap"])


def test_the_cap_passed_from_execute_is_used_without_reading_it_again(monkeypatch):
    chain = _install(monkeypatch, _Chain(auth=None))

    asyncio.run(
        execution_svc._settle_v2(
            "tsk_v2_cap_passed",
            time.monotonic(),
            _plan((0.01, 0.01)),
            payer=PAYER,
            auth_id_hex=AUTH_ID_HEX,
            delivered_steps=frozenset({0, 1}),
            authorized_max=150_000,
        )
    )

    [settle] = chain.named("settle")
    assert sum(p["amount"] for p in _payouts(settle)) == 150_000
    assert (ESCROW, "authorization") not in chain.reads


def test_payout_plan_merges_per_agent_past_sixteen_and_refuses_past_sixteen_agents():
    many_steps = _plan(tuple(0.01 for _ in range(20)), tuple(f"agt_{i % 4}" for i in range(20)))
    merged = execution_svc._payout_plan(many_steps, frozenset(range(20)), {})
    assert [(p.agent_id, p.amount) for p in merged.payouts] == [(f"agt_{i}", 500_000) for i in range(4)]
    assert all(s.amount == 100_000 and s.payout_index == int(s.agent_id[-1]) for s in merged.steps)

    per_step = execution_svc._payout_plan(many_steps, frozenset(range(16)), {})
    assert len(per_step.payouts) == 16

    distinct = _plan(tuple(0.01 for _ in range(17)))
    with pytest.raises(execution_svc._PayoutRefused):
        execution_svc._payout_plan(distinct, frozenset(range(17)), {})


# ── S1: only agents with a confirmed on-chain owner are named ───────────
def test_a_step_whose_agent_has_no_onchain_owner_is_not_paid(monkeypatch, store):
    """The seeded catalogue is not on-chain, and one payout naming it reverts
    the whole settle. It is left out, its share returns to the buyer, and the
    settlement says why — and no dispute can credit what was never charged."""
    chain = _install(monkeypatch, _Chain(auth=_auth(), owners={"agt_01h8": None, "agt_blip": RuntimeError("rpc")}))
    _workers(monkeypatch, {"agt_01h8": _Ok(), "ext_op": _Ok(), "agt_blip": _Ok()})

    _run(_plan((0.012, 0.02, 0.03), ("agt_01h8", "ext_op", "agt_blip")), "tsk_v2_seeded")

    [settle] = chain.named("settle")
    assert _payouts(settle) == [{"agent_id": "ext_op", "amount": 200_000}]
    record = store.recorded[0]
    seeded, paid, blip = record.steps
    assert (seeded.delivered, seeded.paid_usdc, seeded.price_usdc, seeded.unpaid_reason) == (
        True,
        0.0,
        0.0,
        "no_onchain_owner",
    )
    assert (paid.paid_usdc, paid.receipt_id_hex, paid.unpaid_reason) == (0.02, _receipt(0).hex(), None)
    assert blip.unpaid_reason == "owner_unreadable"
    with pytest.raises(dispute_svc.DisputeError) as refused:
        dispute_svc._disputable_step(record, 0)
    assert refused.value.code == "nothing_was_charged"


def test_owner_reads_are_cached_per_agent(monkeypatch):
    chain = _install(monkeypatch, _Chain())

    for _ in range(3):
        assert execution_svc._onchain_owner_sync("ext_op") == OWNER

    assert chain.reads.count((REGISTRY, "owner_of")) == 1


# ── zero delivered ──────────────────────────────────────────────────────
def test_zero_delivered_is_an_empty_settle_under_v2(monkeypatch, store):
    """Custody is released in full, now, instead of sitting until expiry."""
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    _workers(monkeypatch, {"agt_0": _Boom()})

    _run(_plan((0.05,)), "tsk_v2_nothing")

    [settle] = chain.named("settle")
    assert _payouts(settle) == []
    assert scval.to_native(settle[2]) == execution_svc.unsettled_job_id("tsk_v2_nothing")
    assert chain.named("seal") == [] and store.recorded == []
    assert state.tasks["tsk_v2_nothing"].settlement == "released"


def test_zero_delivered_is_a_skip_under_v1(monkeypatch, store):
    chain = _install(monkeypatch, _Chain(), version=1)
    _workers(monkeypatch, {"agt_0": _Boom()})

    _run(_plan((0.05,)), "tsk_v2_nothing_v1")

    assert chain.calls == [] and chain.reads == []
    assert state.tasks["tsk_v2_nothing_v1"].settlement == "skipped"


# ── v1 is unchanged ─────────────────────────────────────────────────────
def test_the_v1_charge_goes_out_with_exactly_the_args_it_always_had(monkeypatch, store):
    """Golden: settler, auth id, the dust-floored total, a fresh job id — and
    no owner read, no authorization read, no settle."""
    chain = _install(monkeypatch, _Chain(), version=1)
    job_id = bytes(range(16))
    monkeypatch.setattr(execution_svc.secrets, "token_bytes", lambda n: job_id)

    asyncio.run(
        execution_svc._settle_and_record(
            "tsk_v2_golden",
            time.monotonic(),
            _plan((0.05, 0.0371234)),
            payer=PAYER,
            auth_id_hex=AUTH_ID_HEX,
            total_usdc=0.0871234,
            delivered_steps=frozenset({0, 1}),
            output_summaries={},
        )
    )

    [charge] = chain.named("charge")
    settler = Keypair.from_secret(SIGNING_SECRET).public_key
    expected = [
        sc.addr(settler),
        sc.bytes16(bytes.fromhex(AUTH_ID_HEX)),
        sc.i128(871_234),
        sc.bytes16(job_id),
    ]
    assert [a.to_xdr() for a in charge] == [a.to_xdr() for a in expected]
    assert chain.named("settle") == [] and chain.reads == []
    assert store.recorded[0].steps[0].paid_usdc is None


# ── receipts reach the seal ─────────────────────────────────────────────
def test_every_payout_receipt_reaches_the_seal_and_the_record(monkeypatch, store):
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    _workers(monkeypatch, {"agt_0": _Ok(), "agt_1": _Ok()})

    _run(_plan((0.01, 0.02)), "tsk_v2_receipts")

    [seal] = chain.named("seal")
    assert scval.to_native(seal[5]) == [_receipt(0), _receipt(1)]
    assert scval.to_native(seal[6]) == 300_000  # Σ payouts, not the plan's estimate
    record = store.recorded[-1]
    assert [s.receipt_id_hex for s in record.steps] == [_receipt(0).hex(), _receipt(1).hex()]
    assert record.proof_tx == SEAL_TX


def test_a_seal_that_did_not_confirm_is_not_kept_as_the_proof(monkeypatch, store):
    """S7: a rejected hash is evidence of nothing."""
    _install(monkeypatch, _Chain(auth=_auth(), seal={"hash": SEAL_TX, "status": "FAILED"}))
    _workers(monkeypatch, {"agt_0": _Ok()})

    _run(_plan((0.01,)), "tsk_v2_bad_seal")

    assert state.tasks["tsk_v2_bad_seal"].proof_tx is None
    assert all(r.proof_tx is None for r in store.recorded)


# ── unknown outcomes ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "answer",
    [
        {"hash": SETTLE_TX, "status": "timeout"},
        sc.InFlightError("poll failed: dropped", SETTLE_TX),
    ],
    ids=["timed-out", "in-flight"],
)
def test_an_unconfirmed_settle_is_never_retried(monkeypatch, store, answer):
    """It may still land. One settle, no seal, no record, no release, and the
    run says `unconfirmed` rather than `failed`."""
    chain = _install(monkeypatch, _Chain(auth=_auth(), settle=answer))
    _workers(monkeypatch, {"agt_0": _Ok()})

    _run(_plan((0.01,)), "tsk_v2_unconfirmed")

    assert [name for name, _ in chain.calls] == ["settle"]
    assert store.recorded == []
    task = state.tasks["tsk_v2_unconfirmed"]
    assert (task.settlement, task.charge_tx) == ("unconfirmed", None)


def test_a_rejected_settle_is_failed_and_keeps_no_hash(monkeypatch, store):
    chain = _install(monkeypatch, _Chain(auth=_auth(), settle={"hash": SETTLE_TX, "status": "FAILED"}))
    _workers(monkeypatch, {"agt_0": _Ok()})

    _run(_plan((0.01,)), "tsk_v2_rejected")

    # The rejected settle, then the release of the custody it left behind.
    assert [_payouts(args) for _, args in chain.calls] == [[{"agent_id": "agt_0", "amount": 100_000}], []]
    task = state.tasks["tsk_v2_rejected"]
    assert (task.settlement, task.charge_tx) == ("failed", None)


@pytest.mark.parametrize("code", [6, 7], ids=["reclaimed", "already-settled"])
def test_a_settle_that_lost_the_race_is_not_settled_by_us(monkeypatch, store, code):
    """Settle is allowed past expiry until the payer reclaims; losing that race
    is refused as Revoked (or Replay). Nothing moved, nothing to release."""
    error = sc.ContractError(f"prepare failed: HostError: Error(Contract, #{code})", code)
    chain = _install(monkeypatch, _Chain(auth=_auth(expires_at=0), settle=error))
    _workers(monkeypatch, {"agt_0": _Ok()})

    _run(_plan((0.01,)), "tsk_v2_race")

    assert [name for name, _ in chain.calls] == ["settle"]
    assert state.tasks["tsk_v2_race"].settlement == "failed"
    assert any(line.msg.startswith("not settled by us") for line in state.traces["tsk_v2_race"])


# ── S4: custody is never stranded ───────────────────────────────────────
def test_a_settle_refused_before_submitting_releases_the_custody(monkeypatch, store):
    monkeypatch.setattr(settings, "max_charge_usdc", 0.001)
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    _workers(monkeypatch, {"agt_0": _Ok()})

    _run(_plan((0.01,)), "tsk_v2_refused")

    [release] = chain.named("settle")
    assert _payouts(release) == []
    assert state.tasks["tsk_v2_refused"].settlement == "failed"


def test_a_settle_the_simulation_refused_releases_the_custody(monkeypatch, store):
    calls: list[list[Any]] = []

    async def invoke(contract_id, function_name, args):
        calls.append(scval.to_native(args[3]))
        if len(calls) == 1:
            raise sc.ContractError("prepare failed: HostError: Error(Contract, #5)", 5)
        return {"hash": SETTLE_TX, "status": "SUCCESS", "result": []}

    _install(monkeypatch, _Chain(auth=_auth()))
    monkeypatch.setattr(sc, "invoke_with_server_key_async", invoke)
    _workers(monkeypatch, {"agt_0": _Ok()})

    _run(_plan((0.01,)), "tsk_v2_sim_refused")

    assert calls == [[{"agent_id": "agt_0", "amount": 100_000}], []]


def test_a_cancelled_run_releases_the_custody(monkeypatch, store):
    chain = _install(monkeypatch, _Chain(auth=_auth()))
    _workers(monkeypatch, {"agt_0": _Hangs()})
    _add_task("tsk_v2_cancel")

    async def scenario() -> None:
        run = asyncio.create_task(
            execution_svc._run(_plan((0.01,)), "tsk_v2_cancel", auth_id_hex=AUTH_ID_HEX, payer=PAYER)
        )
        await asyncio.sleep(0.05)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(scenario())

    [release] = chain.named("settle")
    assert _payouts(release) == []
    assert state.tasks["tsk_v2_cancel"].settlement == "released"


def test_release_authorization_is_an_empty_settle_on_v2(monkeypatch):
    chain = _install(monkeypatch, _Chain())

    assert asyncio.run(execution_svc.release_authorization(AUTH_ID_HEX, reason="test")) == SETTLE_TX
    [release] = chain.named("settle")
    assert _payouts(release) == []
    assert scval.to_native(release[1]) == bytes.fromhex(AUTH_ID_HEX)


def test_release_authorization_is_a_no_op_on_v1(monkeypatch):
    chain = _install(monkeypatch, _Chain(), version=1)

    assert asyncio.run(execution_svc.release_authorization(AUTH_ID_HEX, reason="test")) is None
    assert chain.calls == []


@pytest.mark.parametrize(
    "answer",
    [
        {"hash": SETTLE_TX, "status": "timeout"},
        {"hash": SETTLE_TX, "status": "FAILED"},
        sc.ContractError("prepare failed", 7),
        sc.InFlightError("send failed", SETTLE_TX),
        RuntimeError("anything"),
    ],
)
def test_release_authorization_returns_none_and_never_retries_or_raises(monkeypatch, caplog, answer):
    chain = _install(monkeypatch, _Chain(settle=answer))

    with caplog.at_level(logging.INFO, logger="app.services.execution_svc"):
        assert asyncio.run(execution_svc.release_authorization(AUTH_ID_HEX, reason="test")) is None

    assert len(chain.named("settle")) == 1
    assert any(AUTH_ID_HEX in r.getMessage() for r in caplog.records)


def test_release_authorization_never_raises_on_a_bad_id(monkeypatch):
    chain = _install(monkeypatch, _Chain())

    assert asyncio.run(execution_svc.release_authorization("not-hex", reason="test")) is None
    assert chain.calls == []


# ── version detection ───────────────────────────────────────────────────
def test_an_unreadable_version_settles_as_v1_and_is_not_cached(monkeypatch):
    """Safe means: never a v2 settle on a guess, today's v1 path on a v1
    escrow, and a v1 charge on a v2 escrow is refused at simulation before
    anything is sent. The next run asks again."""
    _install(monkeypatch, _Chain())
    monkeypatch.setattr(sc, "_escrow_versions", {})
    reads: list[str] = []

    def unreadable(contract_id: str) -> int:
        reads.append(contract_id)
        raise RuntimeError("rpc down")

    monkeypatch.setattr(sc, "escrow_version", unreadable)

    assert asyncio.run(execution_svc._escrow_version()) == 1
    assert asyncio.run(execution_svc._escrow_version()) == 1
    assert reads == [ESCROW, ESCROW]


# ── the execute guard ───────────────────────────────────────────────────
def _plan_for_execute(steps: int = 6) -> StoredPlan:
    plan = _plan(tuple(0.01 for _ in range(steps)))
    return plan.model_copy(update={"created_at": time.time()})


def _execute(plan: StoredPlan, payer: str = PAYER) -> str:
    return asyncio.run(execution_svc.execute_plan(plan, auth_id_hex=AUTH_ID_HEX, payer=payer))


@pytest.fixture()
def captured_runs(monkeypatch) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []

    async def fake_run(plan, task_id, **kwargs):
        runs.append(kwargs)

    monkeypatch.setattr(execution_svc, "_run", fake_run)
    return runs


def test_execute_refuses_an_authorization_that_expires_before_a_worst_case_run(monkeypatch, captured_runs):
    needed = execution_svc.worst_case_run_seconds(6)
    _install(monkeypatch, _Chain(auth=_auth(expires_at=int(time.time() + needed - 30))))

    with pytest.raises(execution_svc.AuthorizationRefusedError) as refused:
        _execute(_plan_for_execute())

    assert (refused.value.status_code, refused.value.detail) == (409, "authorization_expiring")
    assert captured_runs == []


def test_execute_passes_an_authorization_that_covers_the_run(monkeypatch, captured_runs):
    needed = execution_svc.worst_case_run_seconds(6)
    _install(monkeypatch, _Chain(auth=_auth(expires_at=int(time.time() + needed + 60))))

    _execute(_plan_for_execute())

    assert captured_runs == [{"auth_id_hex": AUTH_ID_HEX, "payer": PAYER, "authorized_max": 10_000_000}]


def test_the_worst_case_for_the_largest_plan_is_what_the_docs_say():
    assert execution_svc.worst_case_run_seconds(6) == pytest.approx(
        settings.reputation_batch_timeout_seconds + 6 * 125.0 + 150.0
    )


@pytest.mark.parametrize(
    ("auth", "payer", "status", "code"),
    [
        (_auth(payer=Keypair.random().public_key), PAYER, 403, "authorization_payer_mismatch"),
        (_auth(agent_id="pln_other"), PAYER, 409, "authorization_plan_mismatch"),
        (_auth(settled=True), PAYER, 409, "authorization_spent"),
        (_auth(revoked=True), PAYER, 409, "authorization_spent"),
        (_auth(max_amount=599_999), PAYER, 409, "authorization_insufficient"),
        (None, PAYER, 404, "authorization_not_found"),
    ],
    ids=["payer", "plan", "settled", "reclaimed", "insufficient", "missing"],
)
def test_execute_refuses_an_authorization_that_cannot_pay_for_the_plan(
    monkeypatch, captured_runs, auth, payer, status, code
):
    _install(monkeypatch, _Chain(auth=auth))

    with pytest.raises(execution_svc.AuthorizationRefusedError) as refused:
        _execute(_plan_for_execute(), payer=payer)

    assert (refused.value.status_code, refused.value.detail) == (status, code)
    assert captured_runs == []


def test_execute_refuses_an_unreadable_authorization(monkeypatch, captured_runs):
    chain = _install(monkeypatch, _Chain())

    def down(*a, **k):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(sc, "simulate_read", down)

    with pytest.raises(execution_svc.AuthorizationRefusedError) as refused:
        _execute(_plan_for_execute())

    assert (refused.value.status_code, refused.value.detail) == (503, "authorization_unreadable")
    assert chain.calls == []


def test_execute_reads_nothing_on_v1(monkeypatch, captured_runs):
    chain = _install(monkeypatch, _Chain(), version=1)

    _execute(_plan_for_execute())

    assert chain.reads == []
    assert captured_runs[0]["authorized_max"] is None


# ── the settlement state on the receipt, and the plan total ─────────────
def test_the_receipt_reports_the_settlement_state():
    from app.routers import disputes

    state.add_task(Task(id="tsk_v2_receipt", intent="x", agents=1, spent=0.0, status="complete", settlement="failed"))
    record = SettlementRecord(
        task_id="tsk_v2_receipt",
        payer=PAYER,
        auth_id_hex=AUTH_ID_HEX,
        job_id_hex="11" * 16,
        charge_tx=SETTLE_TX,
        proof_tx=None,
        settled_usdc=0.02,
        steps=(
            dispute_store.SettlementStep(
                0, "agt_01h8", None, 0.0, True, paid_usdc=0.0, unpaid_reason="no_onchain_owner"
            ),
        ),
        settled_at=1.0,
        window_closes_at=2.0,
    )

    assert disputes._settlement_state("tsk_v2_receipt", None) == "failed"
    assert disputes._settlement_state("tsk_v2_receipt", record) == "settled"
    assert disputes._settlement_state("tsk_v2_unknown", None) is None
    view = disputes.SettlementView.of(record).steps[0]
    assert (view.paid_usdc, view.unpaid_reason, view.creditable_usdc) == (0.0, "no_onchain_owner", 0.0)


def test_the_plan_total_is_exactly_what_paying_every_step_moves():
    """S6: the console signs `total_usdc` as the cap. Rounded to four decimals
    it fell a stroop-count short of the payouts and the settle reverted."""
    from app.services.orchestrator_svc import authorizable_total_usdc

    steps = _plan((0.012, 0.0250002, 0.0000001)).plan.steps
    total = authorizable_total_usdc(steps)

    assert sc.usdc_to_i128(total) == sum(sc.usdc_to_i128(s.est_price_usdc) for s in steps) == 370_003
