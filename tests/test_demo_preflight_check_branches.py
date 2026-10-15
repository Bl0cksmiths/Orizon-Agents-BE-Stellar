"""Branch-level characterization of the demo pre-flight checks.

Each test calls one check directly, against `healthy_world()` answering
through a real `Reads`, and pins the exact status, detail and fix an
operator reads. A scripted outcome per path stands in for a service that
gives no answer (`Unreachable`) or answers a status the fake never would.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from scripts.demo_preflight.api import Reads
from scripts.demo_preflight.checks import (
    FAIL,
    PASS,
    SKIPPED,
    Check,
    Facts,
    check_below_floor,
    check_external_ready,
    check_routable_count,
    read_backend_readiness,
)
from scripts.demo_preflight.fakes import API, BACKEND, FRONTEND, OP1, OP2, FakeWorld, healthy_world, ready_steps, rep
from scripts.demo_preflight.retry import RetryPolicy

DOWN = "down"  # a scripted outcome: the transport raises, so the read is Unreachable


def _reads(world: FakeWorld, script: dict[str, list[Any]] | None = None) -> tuple[Reads, list[float]]:
    """A `Reads` over `world`, where `script[path]` overrides that path's answers in order.

    Each scripted item is `DOWN`, an `httpx.Response`, or None (let the world
    answer). Once a path's script runs out, the world answers it.
    """
    queues = {path: list(items) for path, items in (script or {}).items()}
    sleeps: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        queue = queues.get(request.url.path)
        item = queue.pop(0) if queue else None
        if item == DOWN:
            raise httpx.ConnectError("refused", request=request)
        if isinstance(item, httpx.Response):
            return item
        return world.handle(request)

    reads = Reads(
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        api=API,
        backend=BACKEND,
        frontend=FRONTEND,
        retry=RetryPolicy(attempts=1, sleep=lambda _s: None),
        sleep=sleeps.append,
    )
    return reads, sleeps


def _facts(world: FakeWorld, **overrides: Any) -> Facts:
    facts = Facts(warm=True, network=dict(world.network), adoption=world.adoption, agents=world.agents)
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts


def _as_tuple(check: Check) -> tuple[str, str, str]:
    return check.status, check.detail, check.fix


# ── read_backend_readiness ──────────────────────────────────────
def test_readiness_unreachable_names_the_backend_host() -> None:
    world = healthy_world()
    reads, _ = _reads(world, {"/readiness": [DOWN]})
    check = read_backend_readiness(reads, _facts(world))
    assert check.status == FAIL
    assert check.detail.startswith(f"GET {BACKEND}/readiness gave no answer after retries: ConnectError")
    assert check.fix == (
        f"Check --backend ({BACKEND}) is the host orizons.xyz proxies /api to, and that it is deployed."
    )


def test_readiness_that_is_not_a_report_names_the_proxy() -> None:
    world = healthy_world()
    reads, _ = _reads(world, {"/readiness": [httpx.Response(404, json={"error": {"code": "nope"}})]})
    assert _as_tuple(read_backend_readiness(reads, _facts(world))) == (
        FAIL,
        f"{BACKEND}/readiness answered HTTP 404 (nope)",
        f"Pass --backend with the backend's own host; /readiness is root-level and {API} proxies only /api.",
    )


@pytest.mark.parametrize(
    ("change", "missing"),
    [
        ({"disputes": "postgres"}, "disputes"),
        ({"disputes": {"reconcile": {"enabled": True}}}, "disputes.store"),
        ({"disputes": {"store": "postgres", "reconcile": {}}}, "disputes.reconcile"),
        ({"disputes": {"store": "postgres", "reconcile": "on"}}, "disputes.reconcile"),
        ({"disputes": {}}, "disputes.store, disputes.reconcile"),
        ({"escrow": {"contract": "C"}}, "escrow {contract, version}"),
        ({"disputes": None, "escrow": None}, "disputes, escrow {contract, version}"),
    ],
)
def test_readiness_without_the_5_01_fields_names_each_one(change: dict[str, Any], missing: str) -> None:
    world = healthy_world()
    assert world.readiness is not None
    world.readiness.update(change)
    reads, _ = _reads(world)
    facts = _facts(world)
    assert _as_tuple(read_backend_readiness(reads, facts)) == (
        FAIL,
        f"{BACKEND}/readiness has no {missing}: the deployed backend predates story 5.01's readiness fields",
        "Deploy the backend's main (5.01 + 5.02 merged) to the Render service.",
    )
    assert facts.readiness == world.readiness


def test_readiness_that_is_not_ready_names_each_dependency() -> None:
    world = healthy_world()
    assert world.readiness is not None
    world.readiness.update(status="degraded", llm="missing", stellar="partial")
    reads, _ = _reads(world)
    assert _as_tuple(read_backend_readiness(reads, _facts(world))) == (
        FAIL,
        "status 'degraded' (llm 'missing', stellar 'partial')",
        "Set the missing configuration on the Render dashboard: OPENAI_API_KEY for `llm`, the contract ids, "
        "STELLAR_RPC_URL and STELLAR_ADMIN_ADDRESS for `stellar`.",
    )


def test_readiness_of_another_deployment_names_both_escrows() -> None:
    world = healthy_world()
    assert world.readiness is not None
    world.readiness["escrow"]["contract"] = "CELSEWHERE"
    reads, _ = _reads(world)
    facts = _facts(world)
    assert _as_tuple(read_backend_readiness(reads, facts)) == (
        FAIL,
        f"/readiness names escrow CELSEWHERE, but {API}/api/stellar/network names {facts.escrow}: "
        "the two hosts are not the same deployment",
        "Pass --backend with the host that --api's /api proxy forwards to.",
    )


def test_ready_readiness_passes_with_its_store() -> None:
    world = healthy_world()
    reads, _ = _reads(world)
    facts = _facts(world, network={})
    assert _as_tuple(read_backend_readiness(reads, facts)) == (PASS, "ready; disputes store postgres", "")
    assert facts.readiness == world.readiness


# ── check_external_ready ────────────────────────────────────────
def test_external_ready_names_every_unready_agent_in_order() -> None:
    world = healthy_world()
    world.adoption = world.adoption_payload({OP1: ["alpha", "beta", "gamma", "delta", "eps"], OP2: ["lowrep"]})
    world.agent_readiness["beta"] = {"ready": False, "steps": ready_steps()}
    world.agent_readiness["gamma"] = {"ready": True, "steps": ready_steps()[:3]}
    world.agent_readiness["delta"] = {"ready": True, "steps": ready_steps(bound="failed")}
    reads, _ = _reads(
        world,
        {
            "/api/agents/alpha/readiness": [DOWN],
            "/api/agents/eps/readiness": [httpx.Response(404, json={"error": {"code": "unknown_agent"}})],
        },
    )
    check = check_external_ready(reads, _facts(world, below_floor=["lowrep"]))
    assert check.status == FAIL
    unready = check.detail.removeprefix("not ready: ").split("; ")
    assert unready[0].startswith(
        f"alpha: readiness unreadable (GET {API}/api/agents/alpha/readiness gave no answer after retries"
    )
    assert unready[1:] == [
        "beta: ready is False",
        "gamma: `reachable` is missing (the readiness answer has no such step)",
        "delta: `bound` is failed (bound is failed) — fix bound",
        "eps: readiness answered HTTP 404 (unknown_agent)",
    ]
    assert check.fix == (
        "Have each operator fix the named step (their /app/operator page shows the same list), "
        "or have them unbind an agent they no longer run."
    )


def test_external_ready_without_an_external_agent_is_skipped() -> None:
    world = healthy_world()
    reads, _ = _reads(world)
    assert _as_tuple(check_external_ready(reads, _facts(world, adoption=None))) == (
        SKIPPED,
        "there is no external agent to check",
        "Fix operator.external first.",
    )


def test_external_ready_passes_and_sets_the_subject_aside() -> None:
    world = healthy_world()
    reads, _ = _reads(world)
    assert _as_tuple(check_external_ready(reads, _facts(world, below_floor=["lowrep"]))) == (
        PASS,
        "ready and reachable: alpha, beta; below the floor by design (the exclusion subject): lowrep",
        "",
    )
    assert _as_tuple(check_external_ready(reads, _facts(world, adoption=world.adoption_payload({OP1: ["alpha"]})))) == (
        PASS,
        "ready and reachable: alpha",
        "",
    )


# ── check_below_floor ───────────────────────────────────────────
def test_below_floor_is_skipped_until_the_backend_and_agents_are_read() -> None:
    world = healthy_world()
    reads, _ = _reads(world)
    assert _as_tuple(check_below_floor(reads, _facts(world, warm=False))) == (
        SKIPPED,
        "the backend never answered",
        "Fix network.warm first.",
    )
    assert _as_tuple(check_below_floor(reads, _facts(world, agents=None))) == (
        SKIPPED,
        "GET /api/agents was not read",
        "Rerun once the backend answers.",
    )


@pytest.mark.parametrize("path", ["/api/stellar/reputation/params", "/api/stellar/reputation"])
def test_below_floor_unreachable_reputation_fails(path: str) -> None:
    world = healthy_world()
    reads, _ = _reads(world, {path: [DOWN]})
    check = check_below_floor(reads, _facts(world))
    assert check.status == FAIL
    assert check.detail.startswith(f"GET {API}{path} gave no answer after retries: ConnectError")
    assert check.fix == "Rerun; the reputation routes did not answer."


def test_below_floor_unanswered_reputation_names_both_statuses() -> None:
    world = healthy_world()
    reads, _ = _reads(world, {"/api/stellar/reputation": [httpx.Response(500, json={"error": {"code": "boom"}})]})
    assert _as_tuple(check_below_floor(reads, _facts(world))) == (
        FAIL,
        "reputation params HTTP 200, batch HTTP 500 (boom)",
        "Deploy a backend build that serves /api/stellar/reputation and /params.",
    )


def test_below_floor_without_a_floor_fails() -> None:
    world = healthy_world()
    world.params["floor_bps"] = "5500"
    reads, _ = _reads(world)
    facts = _facts(world)
    assert _as_tuple(check_below_floor(reads, facts)) == (
        FAIL,
        "the params carry no floor_bps",
        "Deploy a backend build whose params name the floor.",
    )
    assert facts.floor_bps is None


def test_below_floor_with_routing_off_fails() -> None:
    world = healthy_world()
    world.params["enabled"] = "yes"
    reads, _ = _reads(world)
    assert _as_tuple(check_below_floor(reads, _facts(world))) == (
        FAIL,
        "reputation-gated routing is off: the floor excludes no one",
        "Set REPUTATION_ENABLED=true on the Render dashboard.",
    )


def test_below_floor_reread_that_gives_no_answer_fails() -> None:
    world = healthy_world()
    world.degraded_batches = 1
    reads, sleeps = _reads(world, {"/api/stellar/reputation": [None, DOWN]})
    facts = _facts(world)
    check = check_below_floor(reads, facts)
    assert check.status == FAIL
    assert check.detail.startswith(f"GET {API}/api/stellar/reputation gave no answer after retries")
    assert check.fix == "Rerun; the reputation route did not answer."
    assert sleeps == [5.0]
    assert facts.floor_bps == 5500
    assert facts.reputations is None


def test_below_floor_reread_that_is_refused_names_the_read() -> None:
    world = healthy_world()
    world.degraded_batches = 2
    refused = httpx.Response(500, json={"error": {"code": "boom"}})
    reads, sleeps = _reads(world, {"/api/stellar/reputation": [None, None, refused]})
    assert _as_tuple(check_below_floor(reads, _facts(world))) == (
        FAIL,
        "reputation batch HTTP 500 (boom) on re-read 3",
        "Rerun; the reputation route did not answer.",
    )
    assert sleeps == [5.0, 5.0]


def test_below_floor_passes_naming_what_it_ignored_and_the_rereads() -> None:
    world = healthy_world()
    world.agents.append({**world.agents[1], "id": "gone", "status": "offline"})
    world.agents.append({**world.agents[1], "id": "loose", "bound": False})
    world.agents.append({**world.agents[1], "id": "norep"})
    world.agents.append({**world.agents[1], "id": "nobound"})
    world.reputations.update(
        gone=rep("gone", 1000),
        loose=rep("loose", 1100),
        nobound={**rep("nobound", 1), "lower_bound_bps": None},
        alpha=rep("alpha", 1200, stale=True),
        beta=rep("beta", 1300, degraded=True),
    )
    world.degraded_batches = 1
    reads, _ = _reads(world)
    facts = _facts(world)
    check = check_below_floor(reads, facts)
    assert _as_tuple(check) == (
        PASS,
        "lowrep (4100 < 5500 bps); ignored alpha (1200 bps, stale), beta (1300 bps, degraded), "
        "gone (1000 bps, delisted: not routable, so never excluded), "
        "loose (1100 bps, not bound: not routable, so never excluded)"
        " (after 2 reads: every agent read degraded first, the backend waking)",
        "",
    )
    assert facts.below_floor == ["lowrep"]
    assert facts.lower_bounds == {"lowrep": 4100}
    assert facts.floor_bps == 5500
    assert facts.reputations is not None and set(facts.reputations) == set(world.reputations)


def test_below_floor_fails_naming_what_it_ignored_and_the_degraded_count() -> None:
    world = healthy_world()
    world.reputations.update(lowrep=rep("lowrep", 4100, degraded=True), beta=rep("beta", 6000, degraded=True))
    world.degraded_batches = 1
    reads, _ = _reads(world)
    facts = _facts(world)
    check = check_below_floor(reads, facts)
    assert check.status == FAIL
    assert check.detail == (
        "no registered, bound and listed agent has an on-chain lower bound below 5500 bps"
        "; ignored as not a verdict: lowrep (4100 bps, degraded)"
        "; 2 of 3 registered agents read degraded (the ledger could not be read)"
        " (after 2 reads: every agent read degraded first, the backend waking)"
    )
    assert check.fix == (
        "Give one registered agent a real low record: paid runs rated low, or an upheld dispute, until its "
        "lower bound reads below the floor on GET /api/stellar/reputation with degraded and stale false. "
        "A degraded or stale read is the prior or an old read, never a verdict. The agent must also be bound "
        "(rebind it from its owner wallet) and listed, or the card shows 'no endpoint', not 'excluded'."
    )
    assert facts.below_floor == [] and facts.lower_bounds == {}


def test_below_floor_fails_plainly_with_nothing_ignored() -> None:
    world = healthy_world()
    world.reputations["lowrep"] = rep("lowrep", 5500)
    reads, _ = _reads(world)
    check = check_below_floor(reads, _facts(world))
    assert check.status == FAIL
    assert check.detail == "no registered, bound and listed agent has an on-chain lower bound below 5500 bps"


def test_below_floor_every_read_degraded_fails_with_the_count() -> None:
    world = healthy_world()
    world.degraded_batches = 3
    reads, sleeps = _reads(world)
    facts = _facts(world)
    check = check_below_floor(reads, facts)
    assert check.status == FAIL
    assert check.detail == (
        "every registered agent (3) read degraded on each of 3 reads 5 s apart: the backend could not read "
        "the ReputationLedger, so this is no verdict on any agent, not a finding that none is below the floor"
    )
    assert check.fix == (
        "Check the Soroban RPC the backend reads (/api/stellar/network rpc_url) answers and that the "
        "reputation_ledger id there is right, wait a minute for the backend's ledger read, and rerun. On "
        "camera a degraded read shows '⚠ unverified' and no exclusion at all."
    )
    assert sleeps == [5.0, 5.0]
    assert facts.reputations is None and facts.floor_bps == 5500


# ── check_routable_count ────────────────────────────────────────
def test_routable_count_is_skipped_without_the_batch() -> None:
    world = healthy_world()
    assert _as_tuple(check_routable_count(_facts(world))) == (
        SKIPPED,
        "the agents or the reputation batch were not read",
        "Fix exclusion.below_floor first.",
    )


def test_routable_count_passes_naming_what_it_did_not_count() -> None:
    world = healthy_world()
    world.agents.append({**world.agents[1], "id": "extra"})
    world.agents.append({**world.agents[1], "id": "nobody"})
    world.agents.append({**world.agents[1], "id": "stale1"})
    world.reputations.update(extra=rep("extra", 7000), stale1=rep("stale1", 7000, stale=True))
    facts = _facts(world, reputations=world.reputations, floor_bps=5500)
    assert _as_tuple(check_routable_count(facts)) == (
        PASS,
        "4 clear 5500 bps: agt_01h8, alpha, beta, extra; not counted: nobody (no reputation entry), stale1 (stale)",
        "",
    )


def test_routable_count_fails_with_none_clear() -> None:
    world = healthy_world()
    world.reputations = {a: rep(a, 100, degraded=a == "alpha") for a in world.reputations}
    facts = _facts(world, reputations=world.reputations, floor_bps=5500)
    check = check_routable_count(facts)
    assert _as_tuple(check) == (
        FAIL,
        "only 0 routable agent(s) clear 5500 bps; not counted: alpha (degraded): with fewer than 3, the planner's "
        "backstop re-admits one below the floor, so the card reads 'kept below floor', not 'excluded'",
        "Bind, or relist, agents that clear the floor until at least 3 routable ones do (listed, and seeded or "
        "bound, with a fresh read at or above the floor), then rerun. Retake S05 only after the registry is fixed.",
    )
