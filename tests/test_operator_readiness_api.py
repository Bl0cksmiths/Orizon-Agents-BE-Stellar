"""GET /api/agents/{agent_id}/readiness, end to end (story 5.02).

Every sub-read is replaced at its own seam — the owner read, the registry
record (through the Soroban client itself), reputation, settlement, the
binding store and the socket under the probe's pinned transport — so the
route, the concurrency, the bounds and the cache run for real.

What is pinned here and nowhere else:

  * the response is the frozen contract, byte for byte (the snapshot), and the
    seven keys are always there, in order, whatever broke;
  * the probe fetches the bound URL only, whatever the request carries;
  * no URL, host or path reaches the response or the log;
  * the answer is cached per agent and single-flight;
  * a slow or failing sub-read is `unknown` inside its bound, never a hang and
    never a 500;
  * an id nobody registered costs one owner read and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.workers import external_http
from app.config import settings
from app.main import app
from app.security import RateLimitMiddleware
from app.services import external_binding, registry_sync, reputation_svc, settlement_svc
from app.services import operator_readiness as readiness
from app.services.binding_store import InMemoryBindingStore
from app.services.settlement_svc import SettlementEntry, SettlementEvidence
from app.state import state
from app.stellar import cache as rcache
from app.stellar import client as sc

AGENT = "readiness_api"
OWNER = "GBRPYHIL2CI3FNQ4BXLFMNDLFJUNPU2HY3ZMFSHONUCEOASW7QC7OX2H"
BUYER = "GCBUYERXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
SECRET = "s3cr3t-api-token"
HOST = "operator-agent.example"
BOUND = f"https://{HOST}/dispatch/v1?token={SECRET}"
PUBLIC_V4 = "93.184.216.34"
TX = "cd" * 32
STEP_FIELDS = {"key", "status", "detail", "action", "evidence"}


def _record() -> dict[str, Any]:
    return {
        "id": AGENT,
        "name": "Readiness API",
        "skills": ["research"],
        "price": 500_000,
        "active": True,
        "owner": OWNER,
    }


class World:
    """The scripted state of one agent, and a record of every read taken."""

    def __init__(self) -> None:
        self.owner: str | None = OWNER
        self.probe_status = 200
        self.probe_delay = 0.0
        self.rep = reputation_svc._info_from_state(
            AGENT, {"sum_w": 90 * 100 * 10_000_000, "weight": 10_000_000, "count": 2}
        )
        self.evidence = SettlementEvidence(
            agent_id=AGENT,
            asset="native",
            window_days=7.0,
            scanned_ledgers=120_960,
            entries=[
                SettlementEntry(
                    job_id="00" * 16,
                    auth_id="01" * 16,
                    amount_stroops=500_000,
                    ledger=1234,
                    tx_hash=TX,
                    at=None,
                    payer=BUYER,
                    self_payment=False,
                    exclusion=None,
                )
            ],
            total_stroops=500_000,
            self_payment_stroops=0,
            truncated=False,
            unavailable=None,
        )
        self.delays: dict[str, float] = {}
        self.raises: dict[str, BaseException] = {}
        self.calls: dict[str, int] = {}
        self.requests: list[httpx.Request] = []
        self.store = InMemoryBindingStore()

    async def _step(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.delays:
            await asyncio.sleep(self.delays[name])
        if name in self.raises:
            raise self.raises[name]


@pytest.fixture()
def world(monkeypatch):
    rcache.clear()
    w = World()
    asyncio.run(w.store.put(AGENT, BOUND, OWNER))
    monkeypatch.setattr(settings, "stellar_agent_registry", "CREGISTRY")
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CLEDGER")

    async def _owner(agent_id: str) -> str | None:
        await w._step("owner")
        return w.owner if agent_id == AGENT else None

    def _simulate(contract_id: str, fn: str, args: Any = None, *a: Any, **kw: Any) -> Any:
        w.calls[f"simulate:{fn}"] = w.calls.get(f"simulate:{fn}", 0) + 1
        if fn == "get" and "registry" not in w.raises:
            if "registry" in w.delays:
                time.sleep(w.delays["registry"])
            return _record()
        raise RuntimeError("simulate failed: not scripted")

    async def _rep(agent_id: str) -> reputation_svc.RepInfo:
        await w._step("reputation")
        return w.rep

    async def _settlement(agent_id: str) -> SettlementEvidence:
        await w._step("settlement")
        return w.evidence

    class _Store:
        async def get(self, agent_id: str) -> Any:
            await w._step("binding")
            return await w.store.get(agent_id)

    async def _respond(request: httpx.Request) -> httpx.Response:
        w.requests.append(request)
        if w.probe_delay:
            await asyncio.sleep(w.probe_delay)
        return httpx.Response(w.probe_status, text=f"body echoing {BOUND}", headers={"x-echo": BOUND})

    async def _resolve(host: str) -> tuple[str, ...]:
        return (PUBLIC_V4,)

    monkeypatch.setattr(external_binding, "resolve_owner", _owner)
    monkeypatch.setattr(sc, "simulate_read", _simulate)
    monkeypatch.setattr(reputation_svc, "fetch_rep", _rep)
    monkeypatch.setattr(settlement_svc, "fetch_settlement", _settlement)
    monkeypatch.setattr(readiness, "get_binding_store", lambda: _Store())
    monkeypatch.setattr(readiness, "_inner_transport", lambda: httpx.MockTransport(_respond))
    monkeypatch.setattr(external_http, "resolve_checked_addresses", _resolve)
    state.add_agent(registry_sync._to_agent(_record()))
    yield w
    state.agents.pop(AGENT, None)
    rcache.clear()


@pytest.fixture()
def api():
    # No lifespan: nothing in this route needs it, and the background loops it
    # starts (registry sync, reputation pre-warm) would read the scripted chain.
    return TestClient(app)


def _get(api: TestClient, agent_id: str = AGENT, **params: str) -> dict[str, Any]:
    response = api.get(f"/api/agents/{agent_id}/readiness", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _statuses(body: dict[str, Any]) -> dict[str, str]:
    return {step["key"]: step["status"] for step in body["steps"]}


# --- the contract ------------------------------------------------------------------


def test_the_response_matches_the_frozen_contract_exactly(world, api):
    world.rep = reputation_svc._prior_info(AGENT)  # a new agent: routable by the prior, never run
    world.evidence = world.evidence.model_copy(update={"entries": [], "total_stroops": 0})

    body = _get(api)

    assert isinstance(body["checked_at"], int)
    assert abs(body["checked_at"] - time.time()) < 60
    body["checked_at"] = 1759046400
    assert body == {
        "agent_id": AGENT,
        "checked_at": 1759046400,
        "ready": True,
        "steps": [
            {
                "key": "registered",
                "status": "done",
                "detail": f"Registered on-chain; owned by {OWNER}.",
                "action": None,
                "evidence": {"explorer": f"https://stellar.expert/explorer/testnet/account/{OWNER}"},
            },
            {
                "key": "active",
                "status": "done",
                "detail": "Active on-chain and listed in the marketplace.",
                "action": None,
                "evidence": None,
            },
            {
                "key": "bound",
                "status": "done",
                "detail": "An HTTPS endpoint is bound to this agent.",
                "action": None,
                "evidence": None,
            },
            {
                "key": "reachable",
                "status": "done",
                "detail": "Your endpoint answered 200.",
                "action": None,
                "evidence": None,
            },
            {
                "key": "routable",
                "status": "done",
                "detail": "Scores 5677 bps against the 5500 bps routing floor: a new agent's prior, which clears "
                "the floor by design.",
                "action": None,
                "evidence": None,
            },
            {
                "key": "first_run",
                "status": "todo",
                "detail": "No step has been dispatched to this agent and rated on-chain yet.",
                "action": "Once the steps above are done, run a wallet-authorized workflow on the Run page whose "
                "goal needs this agent's skill; each step is rated on-chain when the run settles.",
                "evidence": None,
            },
            {
                "key": "first_settlement",
                "status": "todo",
                "detail": "No settled payment to this agent in the last 7 days (Stellar RPC keeps about 7 days "
                "of events).",
                "action": "Run a paid, wallet-authorized workflow on the Run page from a buyer wallet that is not "
                "the agent's owner; the payout settles when the run completes.",
                "evidence": None,
            },
        ],
    }


def test_the_openapi_schema_publishes_the_contract(api):
    schema = api.get("/openapi.json").json()
    operation = schema["paths"]["/api/agents/{agent_id}/readiness"]["get"]
    ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    model = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    step = schema["components"]["schemas"]["ReadinessStep"]

    assert set(model["required"]) == {"agent_id", "checked_at", "ready", "steps"}
    assert set(step["required"]) == STEP_FIELDS
    assert step["properties"]["key"]["enum"] == list(readiness.STEP_KEYS)
    assert step["properties"]["status"]["enum"] == ["done", "todo", "failed", "unknown"]


def test_first_settlement_evidence_on_the_wire(world, api):
    step = _get(api)["steps"][6]

    assert step["status"] == "done"
    assert step["evidence"] == {"tx_hash": TX, "explorer": f"https://stellar.expert/explorer/testnet/tx/{TX}"}


def _break(world: World, how: str) -> None:
    if how == "unregistered":
        world.owner = None
    elif how == "chain_down":
        world.raises["owner"] = RuntimeError("rpc down")
        world.raises["registry"] = RuntimeError("rpc down")
    elif how == "unbound":
        world.store = InMemoryBindingStore()
    elif how == "endpoint_502":
        world.probe_status = 502
    elif how == "everything":
        world.raises.update({k: RuntimeError("x") for k in ("owner", "binding", "reputation", "settlement")})


@pytest.mark.parametrize("how", ["healthy", "unregistered", "chain_down", "unbound", "endpoint_502", "everything"])
def test_all_seven_keys_always_in_order(world, api, how):
    _break(world, how)

    body = _get(api)

    assert [step["key"] for step in body["steps"]] == list(readiness.STEP_KEYS)
    for step in body["steps"]:
        assert set(step) == STEP_FIELDS
        assert step["status"] in {"done", "todo", "failed", "unknown"}
        if step["status"] in {"todo", "failed"}:
            assert step["action"]


def test_ready_follows_the_first_five(world, api):
    assert _get(api)["ready"] is True

    rcache.clear()
    world.probe_status = 502
    body = _get(api)

    assert _statuses(body)["reachable"] == "failed"
    assert body["ready"] is False


def test_an_unknown_id_is_a_200_saying_register(world, api):
    body = _get(api, "nobody_registered_this")

    assert body["ready"] is False
    assert _statuses(body) == {
        "registered": "todo",
        "active": "todo",
        "bound": "todo",
        "reachable": "todo",
        "routable": "todo",
        "first_run": "todo",
        "first_settlement": "todo",
    }


def test_a_malformed_id_is_refused_before_anything_is_read(world, api):
    assert api.get("/api/agents/not-an-id!/readiness").status_code == 422
    assert world.calls == {}


# --- what the probe may touch ---------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"url": "https://169.254.169.254/latest/meta-data"},
        {"endpoint_url": "https://attacker.example/"},
        {"endpoint": "https://127.0.0.1/"},
    ],
)
def test_the_probe_fetches_only_the_bound_url(world, api, params):
    _get(api, **params)

    [request] = world.requests
    assert request.method == "GET"
    assert request.url.host == PUBLIC_V4  # pinned by the SSRF guard
    assert request.extensions["sni_hostname"] == HOST
    assert request.url.raw_path == f"/dispatch/v1?token={SECRET}".encode()


def test_an_unbound_agent_is_never_probed(world, api):
    world.store = InMemoryBindingStore()

    _get(api)

    assert world.requests == []


def test_an_unregistered_id_costs_one_owner_read_and_nothing_else(world, api):
    _get(api, "nobody_registered_this")

    assert world.calls == {"owner": 1, "binding": 1}
    assert world.requests == []


# --- what may be said about it --------------------------------------------------------


@pytest.mark.parametrize("status", [200, 404, 502])
def test_no_url_host_or_path_is_leaked_in_the_response_or_the_log(world, api, caplog, status):
    world.probe_status = status
    caplog.set_level(logging.DEBUG)

    response = api.get(f"/api/agents/{AGENT}/readiness")

    for needle in (HOST, SECRET, "/dispatch/v1", "body echoing", "x-echo"):
        assert needle not in response.text
        assert needle not in caplog.text


def test_the_logs_are_structured(world, api, caplog):
    caplog.set_level(logging.INFO, logger=readiness.__name__)

    _get(api)

    lines = [r.getMessage() for r in caplog.records if r.name == readiness.__name__]
    assert any(line.startswith(f"readiness probe: agent_id={AGENT} outcome=ok status=200 ") for line in lines)
    assert (
        f"readiness: agent_id={AGENT} ready=True registered=done active=done bound=done reachable=done "
        "routable=done first_run=done first_settlement=done"
    ) in lines


# --- cache, single flight -------------------------------------------------------------


def test_concurrent_checks_share_one_computation(world):
    world.probe_delay = 0.1

    async def _burst() -> list[readiness.Readiness]:
        return await asyncio.gather(*(readiness.check_readiness(AGENT) for _ in range(10)))

    results = asyncio.run(_burst())

    assert len(world.requests) == 1
    assert world.calls["owner"] == 1
    assert len({id(r) for r in results}) == 1


def test_the_answer_is_cached_for_the_window_then_refreshed(world, api, monkeypatch):
    first = _get(api)
    second = _get(api)

    assert len(world.requests) == 1
    assert second == first

    monkeypatch.setattr(readiness, "CACHE_TTL_SECONDS", 0.05)
    rcache.clear()
    _get(api)
    time.sleep(0.1)
    _get(api)

    assert len(world.requests) == 3


def test_the_cache_is_per_agent(world, api):
    asyncio.run(world.store.put("readiness_other", BOUND, OWNER))

    _get(api)
    _get(api, "readiness_other")

    assert len(world.requests) == 2


# --- bounds -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("read", "bound_name", "steps"),
    [
        ("owner", "OWNER_READ_TIMEOUT_SECONDS", {"registered", "active", "routable", "first_run", "first_settlement"}),
        ("registry", "CHAIN_READ_TIMEOUT_SECONDS", {"active"}),
        ("reputation", "CHAIN_READ_TIMEOUT_SECONDS", {"routable", "first_run"}),
        ("settlement", "SETTLEMENT_TIMEOUT_SECONDS", {"first_settlement"}),
        ("binding", "BINDING_READ_TIMEOUT_SECONDS", {"bound", "reachable"}),
    ],
)
def test_a_slow_sub_read_is_unknown_within_its_bound(world, monkeypatch, read, bound_name, steps):
    monkeypatch.setattr(readiness, bound_name, 0.2)
    world.delays[read] = 1.5

    async def _timed() -> tuple[readiness.Readiness, float]:
        # Timed inside the loop: a sync read cut off by its bound leaves its
        # worker thread sleeping, and closing the loop waits for that thread.
        started = time.monotonic()
        result = await readiness.check_readiness(AGENT)
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(_timed())

    assert elapsed < 1.0
    statuses = {step.key: step.status for step in result.steps}
    assert {key for key, status in statuses.items() if status == "unknown"} == steps
    assert all(status == "done" for key, status in statuses.items() if key not in steps)


def test_a_slow_settlement_scan_says_it_is_still_running(world, api, monkeypatch):
    monkeypatch.setattr(readiness, "SETTLEMENT_TIMEOUT_SECONDS", 0.1)
    world.delays["settlement"] = 3.0

    step = _get(api)["steps"][6]

    assert step["detail"] == "The settlement scan is still running."


@pytest.mark.parametrize("read", ["owner", "binding", "reputation", "settlement", "registry"])
def test_a_failing_sub_read_is_unknown_never_a_500(world, api, caplog, read):
    """The exception's text quotes the URL here on purpose: only its type may
    reach the log."""
    caplog.set_level(logging.DEBUG)
    world.raises[read] = RuntimeError(f"boom {BOUND}")

    response = api.get(f"/api/agents/{AGENT}/readiness")

    assert response.status_code == 200
    assert "unknown" in {step["status"] for step in response.json()["steps"]}
    assert SECRET not in response.text
    assert SECRET not in caplog.text


def test_a_failure_in_the_whole_computation_is_all_unknown_not_a_500(world, api, monkeypatch):
    async def _explode(agent_id: str) -> readiness.Readiness:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(readiness, "_compute", _explode)

    body = _get(api)

    assert body["ready"] is False
    assert [step["key"] for step in body["steps"]] == list(readiness.STEP_KEYS)
    assert {step["status"] for step in body["steps"]} == {"unknown"}


# --- the service limiter ------------------------------------------------------------------


def test_the_route_is_under_the_service_rate_limiter(world):
    limited = TestClient(RateLimitMiddleware(app, limit=2, window_seconds=60))

    codes = [limited.get(f"/api/agents/{AGENT}/readiness").status_code for _ in range(3)]

    assert codes == [200, 200, 429]
