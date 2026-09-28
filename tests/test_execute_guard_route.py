"""`/execute` behind the authorization guard (story 5.01, seam audit S2, ADR 0011).

`execute_plan` is replaced by a recorder for the refusal tests, because the
question is whether a run STARTS; the simulated-path rating tests at the end
run the real workflow, because there the question is what a run WRITES.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from escrow_fakes import AUTH, ESCROW, OTHER, PAYER, FakeEscrow, install, make_plan, record, use_escrow
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import orchestrator as orchestrator_router
from app.schemas import ExecuteRequest, StoredPlan, Task
from app.security import CodedHTTPException
from app.services import authorization_guard as guard
from app.services import execution_svc, orchestrator_svc
from app.services.execution_svc import CapacityExhaustedError
from app.state import state
from app.stellar import client as sc

PLAN_ID = "pln_0a1b2c3d"
RELEASE_TX = "f" * 64


class RunRecorder:
    """Stands in for `execute_plan`: mints a running task and records what it was asked to run."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.fail = fail

    async def __call__(self, plan: StoredPlan, *, auth_id_hex: str | None = None, payer: str | None = None) -> str:
        self.calls.append((plan.id, auth_id_hex, payer))
        if self.fail is not None:
            raise self.fail
        task_id = f"tsk_{len(self.calls):016x}"
        state.add_task(Task(id=task_id, intent=plan.intent, agents=1, spent=0.0, status="running"))
        return task_id


class ReleaseRecorder:
    """Stands in for the settle lane's `execution_svc.release_authorization`."""

    def __init__(self, answer: str | None = RELEASE_TX, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.answer = answer
        self.fail = fail

    async def __call__(self, auth_id_hex: str, *, reason: str) -> str | None:
        self.calls.append((auth_id_hex, reason))
        if self.fail is not None:
            raise self.fail
        return self.answer


@pytest.fixture(autouse=True)
def guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    use_escrow(monkeypatch, ESCROW)
    guard.forget_versions()
    guard.forget_claims()
    yield
    guard.forget_versions()
    guard.forget_claims()
    state.plans.pop(PLAN_ID, None)


@pytest.fixture()
def runs(monkeypatch: pytest.MonkeyPatch) -> RunRecorder:
    recorder = RunRecorder()
    monkeypatch.setattr(orchestrator_router, "execute_plan", recorder)
    return recorder


@pytest.fixture()
def releases(monkeypatch: pytest.MonkeyPatch) -> ReleaseRecorder:
    recorder = ReleaseRecorder()
    monkeypatch.setattr(execution_svc, "release_authorization", recorder, raising=False)
    return recorder


def live_plan(plan_id: str = PLAN_ID, created_at: float | None = None) -> StoredPlan:
    plan = make_plan(plan_id, created_at=time.time() if created_at is None else created_at)
    state.plans[plan.id] = plan
    return plan


def live_record(**overrides: Any) -> dict[str, Any]:
    return record(now=time.time(), **overrides)


def paid(client: TestClient, plan_id: str = PLAN_ID, auth: str = AUTH, payer: str = PAYER) -> Any:
    return client.post("/api/orchestrator/execute", json={"plan_id": plan_id, "auth_id_hex": auth, "payer": payer})


# ── the route and `execute_plan`'s single verifier ──────────────────────


def settle_lane_refusal(status: int, code: str) -> Exception:
    """What `execute_plan`'s own check raises (`AuthorizationRefusedError`, a coded HTTP exception)."""
    return CodedHTTPException(status, code, "refused by execute_plan's authorization check")


def test_a_v2_execute_reads_only_the_version_before_execute_plan(
    client: TestClient, monkeypatch, runs, releases
) -> None:
    """One verifier: the authorization itself is `execute_plan`'s to read, never read twice."""
    escrow = install(monkeypatch, FakeEscrow(auth=RuntimeError("the route must not read the authorization")))
    live_plan()
    r = paid(client)
    assert r.status_code == 200, r.text
    assert r.json()["task_id"] == "tsk_0000000000000001"
    assert runs.calls == [(PLAN_ID, AUTH, PAYER)]
    assert escrow.calls == [(ESCROW, "version")]
    assert guard.claimed_by(AUTH) == "tsk_0000000000000001"
    assert releases.calls == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "authorization_payer_mismatch"),
        (409, "authorization_plan_mismatch"),
        (409, "authorization_spent"),
        (409, "authorization_insufficient"),
        (409, "authorization_expiring"),
        (404, "authorization_not_found"),
        (503, "authorization_unreadable"),
    ],
)
def test_execute_plans_refusal_passes_through_unreleased_and_unclaimed(
    client: TestClient, monkeypatch, releases, status: int, code: str
) -> None:
    """It may be someone else's authorization, or another plan's: never released, and never left claimed."""
    escrow = install(monkeypatch, FakeEscrow())
    live_plan()
    refusing = RunRecorder(fail=settle_lane_refusal(status, code))
    monkeypatch.setattr(orchestrator_router, "execute_plan", refusing)
    r = paid(client)
    assert r.status_code == status
    body = r.json()
    assert body["detail"] == code and body["error"]["code"] == code
    assert "release_tx_hash" not in body
    assert releases.calls == [] and (ESCROW, "authorization") not in escrow.calls
    assert guard.claimed_by(AUTH) is None
    # So the same authorization is not refused as "used" when the buyer retries.
    assert paid(client).json()["error"]["code"] == code
    assert len(refusing.calls) == 2


def test_an_unreadable_version_is_503_and_never_runs(client: TestClient, monkeypatch, runs, releases) -> None:
    """Never guessed as v1: that is the reading under which `execute_plan` skips its own check."""
    install(monkeypatch, FakeEscrow(version=ConnectionError("rpc down")))
    live_plan()
    r = paid(client)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "authorization_unreadable")
    assert runs.calls == [] and releases.calls == []
    assert guard.claimed_by(AUTH) is None


def test_half_an_authorization_is_refused_not_run_simulated(client: TestClient, runs) -> None:
    live_plan()
    for body in ({"plan_id": PLAN_ID, "auth_id_hex": AUTH}, {"plan_id": PLAN_ID, "payer": PAYER}):
        r = client.post("/api/orchestrator/execute", json=body)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "authorization_incomplete")
    assert runs.calls == []


# ── v1 is unchanged ─────────────────────────────────────────────────────


def test_v1_runs_exactly_as_before(client: TestClient, monkeypatch, runs, releases) -> None:
    """Against v1 nothing is verified, claimed or released: any auth runs, as often as it is sent."""
    escrow = install(monkeypatch, FakeEscrow(version=None, auth=RuntimeError("never read")))
    live_plan()
    upper = AUTH.upper()
    assert paid(client, auth=upper, payer=OTHER).status_code == 200
    assert paid(client, auth=upper, payer=OTHER).status_code == 200
    # Handed on exactly as sent — the case included — as it always was.
    assert runs.calls == [(PLAN_ID, upper, OTHER), (PLAN_ID, upper, OTHER)]
    assert guard.claimed_by(AUTH) is None and releases.calls == []
    assert escrow.calls == [(ESCROW, "version")]


def test_v1_refusals_keep_their_old_envelopes(client: TestClient, monkeypatch, releases) -> None:
    install(monkeypatch, FakeEscrow(version=None))
    r = paid(client, plan_id="pln_nowhere")
    assert r.status_code == 404 and r.json()["detail"] == "unknown plan_id: pln_nowhere"
    assert "release_tx_hash" not in r.json()

    live_plan(created_at=time.time() - settings.plan_ttl_seconds - 60)
    r = paid(client)
    assert (r.status_code, r.json()["detail"]) == (410, "plan_expired")
    assert "release_tx_hash" not in r.json()

    live_plan()
    monkeypatch.setattr(orchestrator_router, "execute_plan", RunRecorder(fail=CapacityExhaustedError("full")))
    r = paid(client)
    assert (r.status_code, r.json()["detail"]) == (503, "capacity_exhausted")
    assert "release_tx_hash" not in r.json()
    assert releases.calls == []


# ── one authorization, one task ─────────────────────────────────────────


def test_a_second_execute_against_one_authorization_is_refused(client: TestClient, monkeypatch, runs) -> None:
    install(monkeypatch, FakeEscrow())
    live_plan()
    assert paid(client).status_code == 200
    r = paid(client, auth=AUTH.upper())  # the same authorization, spelled differently
    assert (r.status_code, r.json()["error"]["code"]) == (409, "authorization_used")
    assert len(runs.calls) == 1


def test_the_claim_outlives_the_run(client: TestClient, monkeypatch, runs) -> None:
    """A finished run whose settle never landed leaves the authorization unsettled; it still may not run twice."""
    install(monkeypatch, FakeEscrow())
    live_plan()
    task_id = paid(client).json()["task_id"]
    state.tasks[task_id].status = "failed"
    assert paid(client).json()["error"]["code"] == "authorization_used"
    assert len(runs.calls) == 1


class SlowEscrow(FakeEscrow):
    """Holds each read long enough for a second request to reach the same point."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        time.sleep(0.2)
        return super().__call__(*args, **kwargs)


def test_two_concurrent_executes_against_one_authorization_start_exactly_one_run(monkeypatch, runs) -> None:
    """The version read is the await between the claim check and the claim: exactly where two would interleave."""
    install(monkeypatch, SlowEscrow())
    live_plan()
    req = ExecuteRequest(plan_id=PLAN_ID, auth_id_hex=AUTH, payer=PAYER)

    async def both() -> list[Any]:
        return await asyncio.gather(
            orchestrator_router.orchestrator_execute(req),
            orchestrator_router.orchestrator_execute(req),
            return_exceptions=True,
        )

    outcomes = asyncio.run(both())
    started = [o for o in outcomes if not isinstance(o, BaseException)]
    refused = [o for o in outcomes if isinstance(o, guard.AuthorizationRefused)]
    assert len(started) == 1 and len(refused) == 1
    assert refused[0].code == "authorization_used"
    assert len(runs.calls) == 1
    assert guard._locks == {} and guard._lock_users == {}  # nothing left behind


class SlowRun(RunRecorder):
    async def __call__(self, plan: StoredPlan, *, auth_id_hex: str | None = None, payer: str | None = None) -> str:
        await asyncio.sleep(0.3)
        return await super().__call__(plan, auth_id_hex=auth_id_hex, payer=payer)


def test_the_claim_is_pending_while_execute_plan_runs(monkeypatch) -> None:
    install(monkeypatch, FakeEscrow())
    live_plan()
    seen: list[str | None] = []

    async def peek(plan: StoredPlan, *, auth_id_hex: str | None = None, payer: str | None = None) -> str:
        seen.append(guard.claimed_by(AUTH))
        return "tsk_peeked"

    monkeypatch.setattr(orchestrator_router, "execute_plan", peek)
    asyncio.run(
        orchestrator_router.orchestrator_execute(ExecuteRequest(plan_id=PLAN_ID, auth_id_hex=AUTH, payer=PAYER))
    )
    assert seen == [guard.PENDING]
    assert guard.claimed_by(AUTH) == "tsk_peeked"


def test_different_authorizations_do_not_wait_on_each_other(monkeypatch) -> None:
    other_auth = "cd" * 16
    install(monkeypatch, FakeEscrow())
    live_plan()
    slow = SlowRun()
    monkeypatch.setattr(orchestrator_router, "execute_plan", slow)

    async def both() -> list[Any]:
        return await asyncio.gather(
            orchestrator_router.orchestrator_execute(ExecuteRequest(plan_id=PLAN_ID, auth_id_hex=AUTH, payer=PAYER)),
            orchestrator_router.orchestrator_execute(
                ExecuteRequest(plan_id=PLAN_ID, auth_id_hex=other_auth, payer=PAYER)
            ),
        )

    asyncio.run(guard.escrow_version())  # cached, so the only wait left is the run itself
    began = time.monotonic()
    asyncio.run(both())
    # 300 ms each: side by side about 300 ms, one after the other at least 600 ms.
    assert time.monotonic() - began < 0.5
    assert len(slow.calls) == 2


def test_claims_are_bounded_and_never_evict_a_running_task(monkeypatch) -> None:
    monkeypatch.setattr(guard, "MAX_CLAIMS", 3)
    state.add_task(Task(id="tsk_running", intent="x", agents=1, spent=0.0, status="running"))
    guard.claim("a" * 32, "tsk_running")
    for i in range(3):
        guard.claim(f"{i}" * 32, f"tsk_gone_{i}")
    assert len(guard._claims) == 3
    assert guard.claimed_by("a" * 32) == "tsk_running"
    assert guard.claimed_by("0" * 32) is None


def test_a_pending_claim_is_never_evicted(monkeypatch) -> None:
    monkeypatch.setattr(guard, "MAX_CLAIMS", 2)
    guard.claim("a" * 32)
    guard.claim("b" * 32, "tsk_gone_b")
    guard.claim("c" * 32, "tsk_gone_c")
    assert guard.claimed_by("a" * 32) == guard.PENDING
    assert guard.claimed_by("b" * 32) is None


# ── custody release on a refusal ────────────────────────────────────────


def test_an_unknown_plan_releases_a_verified_authorization(client: TestClient, monkeypatch, runs, releases) -> None:
    install(monkeypatch, FakeEscrow(auth=live_record(agent_id="pln_9e5a11ed")))
    r = paid(client, plan_id="pln_9e5a11ed")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "unknown plan_id: pln_9e5a11ed"
    assert body["release_tx_hash"] == RELEASE_TX
    assert "returned to your wallet" in body["error"]["message"]
    assert releases.calls == [(AUTH, "plan_unknown")]
    assert runs.calls == []


def test_an_expired_plan_releases_even_when_its_authorization_no_longer_fits(
    client: TestClient, monkeypatch, runs, releases
) -> None:
    """Ownership alone decides it: the fit checks would otherwise refuse first and strand the custody."""
    install(monkeypatch, FakeEscrow(auth=live_record(max_amount=1, expires_at=int(time.time()) + 5)))
    live_plan(created_at=time.time() - settings.plan_ttl_seconds - 60)
    r = paid(client)
    assert r.status_code == 410
    body = r.json()
    assert (body["detail"], body["error"]["code"], body["release_tx_hash"]) == (
        "plan_expired",
        "plan_expired",
        RELEASE_TX,
    )
    assert "build a fresh plan" in body["error"]["message"]
    assert releases.calls == [(AUTH, "plan_expired")]
    assert runs.calls == []


def test_a_plan_expiring_inside_execute_plan_releases(client: TestClient, monkeypatch, releases) -> None:
    install(monkeypatch, FakeEscrow(auth=live_record()))
    live_plan()
    monkeypatch.setattr(orchestrator_router, "execute_plan", RunRecorder(fail=execution_svc.PlanExpiredError(PLAN_ID)))
    r = paid(client)
    assert (r.status_code, r.json()["release_tx_hash"]) == (410, RELEASE_TX)
    assert releases.calls == [(AUTH, "plan_expired")]
    assert guard.claimed_by(AUTH) is None


def test_capacity_releases_a_verified_authorization(client: TestClient, monkeypatch, releases) -> None:
    install(monkeypatch, FakeEscrow(auth=live_record()))
    live_plan()
    monkeypatch.setattr(orchestrator_router, "execute_plan", RunRecorder(fail=CapacityExhaustedError("full")))
    r = paid(client)
    assert r.status_code == 503
    body = r.json()
    assert (body["detail"], body["error"]["code"], body["release_tx_hash"]) == (
        "capacity_exhausted",
        "capacity_exhausted",
        RELEASE_TX,
    )
    assert releases.calls == [(AUTH, "capacity_exhausted")]
    assert guard.claimed_by(AUTH) is None


@pytest.mark.parametrize(
    "auth",
    [
        {"payer": OTHER},  # someone else's custody
        {"agent_id": "pln_ffffffff"},  # made for another plan
        {"settled": True},
        {"revoked": True},
    ],
)
def test_an_unowned_or_spent_authorization_is_never_released(
    client: TestClient, monkeypatch, runs, releases, auth: dict[str, Any]
) -> None:
    install(monkeypatch, FakeEscrow(auth=live_record(**auth)))
    r = paid(client)  # PLAN_ID is unknown: the release path
    assert r.status_code == 404 and "release_tx_hash" not in r.json()
    live_plan(created_at=time.time() - settings.plan_ttl_seconds - 60)
    r = paid(client)
    assert r.status_code == 410 and "release_tx_hash" not in r.json()
    assert releases.calls == [] and runs.calls == []


def test_capacity_never_releases_an_unowned_authorization(client: TestClient, monkeypatch, releases) -> None:
    install(monkeypatch, FakeEscrow(auth=live_record(payer=OTHER)))
    live_plan()
    monkeypatch.setattr(orchestrator_router, "execute_plan", RunRecorder(fail=CapacityExhaustedError("full")))
    r = paid(client)
    assert (r.status_code, r.json()["detail"]) == (503, "capacity_exhausted")
    assert "release_tx_hash" not in r.json() and releases.calls == []


def test_an_unreadable_chain_never_releases(client: TestClient, monkeypatch, releases) -> None:
    install(monkeypatch, FakeEscrow(auth=ConnectionError("rpc down")))
    r = paid(client)
    assert r.status_code == 404 and "release_tx_hash" not in r.json()
    assert releases.calls == []


def test_a_claimed_authorization_is_never_released(client: TestClient, monkeypatch, runs, releases) -> None:
    """Its run is still paying out of it — even once its plan has been evicted."""
    install(monkeypatch, FakeEscrow(auth=live_record()))
    live_plan()
    assert paid(client).status_code == 200
    state.plans.pop(PLAN_ID)
    r = paid(client)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "authorization_used")
    assert releases.calls == []


def test_a_release_that_did_not_confirm_says_so(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(execution_svc, "release_authorization", ReleaseRecorder(answer=None), raising=False)
    install(monkeypatch, FakeEscrow(auth=live_record(agent_id="pln_9e5a11ed")))
    body = paid(client, plan_id="pln_9e5a11ed").json()
    assert body["release_tx_hash"] is None
    assert "reclaim" in body["error"]["message"]


def test_a_release_that_raises_never_reaches_the_route(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        execution_svc, "release_authorization", ReleaseRecorder(fail=RuntimeError("boom")), raising=False
    )
    install(monkeypatch, FakeEscrow(auth=live_record()))
    live_plan()
    monkeypatch.setattr(orchestrator_router, "execute_plan", RunRecorder(fail=CapacityExhaustedError("full")))
    r = paid(client)
    assert (r.status_code, r.json()["release_tx_hash"]) == (503, None)


def test_a_build_without_release_authorization_still_answers(
    client: TestClient, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delattr(execution_svc, "release_authorization", raising=False)
    install(monkeypatch, FakeEscrow(auth=live_record(agent_id="pln_9e5a11ed")))
    with caplog.at_level("WARNING", logger="app.services.authorization_guard"):
        r = paid(client, plan_id="pln_9e5a11ed")
    assert (r.status_code, r.json()["release_tx_hash"]) == (404, None)
    # Said as what it is — a build without the release — not as a release that raised.
    assert "no release_authorization in this build" in caplog.text


def test_a_synchronous_release_is_accepted(monkeypatch) -> None:
    monkeypatch.setattr(execution_svc, "release_authorization", lambda a, *, reason: "a" * 64, raising=False)
    assert asyncio.run(guard.release(AUTH, reason="x")) == "a" * 64


# ── the simulated path writes nothing on-chain ──────────────────────────


async def _no_thinking() -> None:
    return None


async def _drain_workflows() -> None:
    while pending := [t for t in execution_svc._background_tasks if not t.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


class ChainSpy:
    """Records every on-chain write a run attempts, and lets none of them out."""

    def __init__(self) -> None:
        self.ratings: list[tuple[Any, ...]] = []
        self.invokes: list[tuple[Any, ...]] = []

    async def submit_rating(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.ratings.append(args)
        return {"status": "SUCCESS", "hash": "0" * 64}

    async def invoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.invokes.append(args)
        return {"status": "FAILED", "hash": "1" * 64}


@pytest.fixture()
def chain(monkeypatch: pytest.MonkeyPatch) -> ChainSpy:
    """A deployment that COULD rate: a signing key and a ledger are configured."""
    spy = ChainSpy()
    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", _no_thinking)
    monkeypatch.setattr(settings, "stellar_signing_key", "S" + "A" * 55)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "C" + "L" * 55)
    monkeypatch.setattr(execution_svc.rating_writer, "config_gap", lambda: None)
    monkeypatch.setattr(sc, "submit_rating_async", spy.submit_rating)
    monkeypatch.setattr(sc, "invoke_with_server_key_async", spy.invoke)
    return spy


def test_a_simulated_run_writes_no_rating_and_nothing_else_on_chain(client: TestClient, chain: ChainSpy) -> None:
    """The audit's rating farm needs no payment at all if a SIMULATED run rates. It does not."""
    plan = client.post("/api/orchestrator/decompose", json={"intent": "calculator web app"}).json()
    task = client.post("/api/orchestrator/execute", json={"plan_id": plan["plan_id"]}).json()
    assert client.portal is not None
    client.portal.call(_drain_workflows)
    assert state.tasks[task["task_id"]].status == "complete"  # the run delivered, so it had something to rate
    assert chain.ratings == [] and chain.invokes == []


def test_the_spy_does_see_a_paid_runs_ratings(client: TestClient, monkeypatch, chain: ChainSpy) -> None:
    """The contrast that gives the test above its teeth: the same run, paid, on v1, rates every step."""
    install(monkeypatch, FakeEscrow(version=None))
    plan = client.post("/api/orchestrator/decompose", json={"intent": "calculator web app"}).json()
    r = paid(client, plan_id=plan["plan_id"])
    assert r.status_code == 200
    assert client.portal is not None
    client.portal.call(_drain_workflows)
    assert len(chain.ratings) == len(plan["steps"])
