"""Reputation wiring tests — read endpoints, plan stamping, floor routing,
and the settler's best-effort rating submission.

The read-endpoint half is deliberately asserted on the parsed HTTP body
rather than on a service model: the routers mirror reputation_svc.RepInfo
into their own ReputationInfo by splatting, and pydantic discards keys the
mirror does not declare without raising, so the only place a dropped field
is observable is the wire."""

from __future__ import annotations

import asyncio
import secrets
import time

import pytest

from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.seed import seed_registry
from app.services import execution_svc, orchestrator_svc
from app.services.reputation_svc import STROOPS_PER_USDC, RepInfo
from app.state import state
from app.stellar import cache as rcache
from app.stellar import client as sc


def _info(agent_id: str, *, smoothed: int, lower: int) -> RepInfo:
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed,
        lower_bound_bps=lower,
        avg_bps=smoothed,
        count=3,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


# ── read endpoints ──────────────────────────────────────────────


def test_reputation_batch_covers_every_seeded_agent(client):
    r = client.get("/api/stellar/reputation")
    assert r.status_code == 200
    body = r.json()

    seeded = {a.id for a in state.list_agents()}
    assert seeded, "registry must be seeded"
    assert set(body["reputations"]) == seeded
    assert body["floor_bps"] == settings.reputation_floor_bps
    assert body["prior_bps"] == settings.reputation_prior_bps
    # Hermetic tests have no chain configured → every entry is the prior. A
    # prior because nothing is deployed is a COLD START, not an outage, and the
    # response has to carry the difference: a dashboard that read this state as
    # degraded would raise an unreadable-ledger alarm on every poll of a
    # perfectly healthy network, which is how operators learn to ignore it.
    for info in body["reputations"].values():
        assert info["source"] == "prior"
        assert info["degraded"] is False
        assert info["smoothed_bps"] == settings.reputation_prior_bps


def test_reputation_single_agent_shape(client):
    r = client.get("/api/stellar/reputation/agt_01h8")
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "agt_01h8"
    assert body["source"] == "prior"
    # Unconfigured ledger, so this prior is a cold start rather than a
    # fallback — same distinction as on the batch, drawn by a separate handler.
    assert body["degraded"] is False
    assert body["smoothed_bps"] == settings.reputation_prior_bps
    assert body["count"] == 0
    assert body["lower_bound_bps"] >= settings.reputation_floor_bps


def test_reputation_params_returns_config(client):
    r = client.get("/api/stellar/reputation/params")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["prior_bps"] == settings.reputation_prior_bps
    assert body["floor_bps"] == settings.reputation_floor_bps
    assert body["prior_weight_usdc"] == settings.reputation_prior_weight_usdc
    assert body["wilson_z"] == 1.0
    # On-chain ReputationLedger v2 decay constants, surfaced read-only.
    assert body["epoch_seconds"] == 604_800
    assert body["decay_bps_per_epoch"] == 9_250
    assert body["max_decay_epochs"] == 96
    # Hermetic tests blank the ledger id and never touch the chain.
    assert body["contract_id"] == ""
    assert body["network"] == settings.stellar_network


def test_reputation_params_not_shadowed_by_agent_route(client):
    # /reputation/params is declared before /reputation/{agent_id}; if the
    # dynamic route captured it, "params" would come back as an agent id.
    r = client.get("/api/stellar/reputation/params")
    assert r.status_code == 200
    body = r.json()
    assert "agent_id" not in body
    assert "smoothed_bps" not in body
    assert "prior_weight_usdc" in body


# ── degradation on the wire ─────────────────────────────────────


@pytest.fixture()
def unreadable_ledger(monkeypatch):
    """Reputation configured against a ledger whose every read fails.

    `app.stellar.cache.get_or_set` is the only seam between reputation_svc and
    Soroban RPC, so patching it there reproduces a hard-down chain while the
    real route runs, offline. The tests below take `client` FIRST so lifespan
    starts against conftest's blank ledger id and this fixture arms it only
    afterwards; STELLAR_AGENT_REGISTRY is never touched here, since setting it
    re-arms the 1.02 sync loop that lifespan fires before any test body runs.
    """
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")

    async def rpc_down(key: str, ttl_seconds: float, producer):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(rcache, "get_or_set", rpc_down)


def test_degraded_batch_reaches_the_client_as_degraded_true(client, unreadable_ledger):
    """AC-1 on the wire: Soroban unreachable → every agent in the batch body
    carries degraded=true and source="prior".

    Asserted on parsed JSON, not on a model, because the loss happens in the
    last step: the router answers with `ReputationInfo(**info.model_dump())`,
    and pydantic drops keys the target model does not declare — silently, with
    no error and no failing type check. If this stops holding, the dashboard
    can no longer tell an outage from a cold-start registry, and an outage is
    exactly when the routing floor fails open and every agent, including ones
    already excluded for bad ratings, reads as comfortably routable.
    """
    r = client.get("/api/stellar/reputation")
    assert r.status_code == 200
    body = r.json()

    seeded = {a.id for a in state.list_agents()}
    assert set(body["reputations"]) == seeded
    for agent_id, info in body["reputations"].items():
        assert info["degraded"] is True, f"{agent_id} lost its degradation flag between the service and the client"
        assert info["source"] == "prior"


def test_degraded_single_agent_reaches_the_client_as_degraded_true(client, unreadable_ledger):
    """The same AC on /reputation/{agent_id}.

    A separate handler with a separate splat of its own, so the batch route
    passing is no evidence at all about this one — the agent detail view reads
    from here, and it is the view an operator opens to ask why a specific
    agent scores what it scores.
    """
    r = client.get("/api/stellar/reputation/agt_01h8")
    assert r.status_code == 200
    body = r.json()

    assert body["agent_id"] == "agt_01h8"
    assert body["degraded"] is True
    assert body["source"] == "prior"


def test_partial_outage_marks_only_the_agents_that_failed(client, monkeypatch):
    """One unreadable agent must not smear degraded=true across the batch —
    nor be hidden by the agents that answered.

    The flag is per agent in the response map because that is the granularity
    an operator acts on: the dashboard marks the one agent whose score is a
    fallback while the rest of the registry keeps its real on-chain numbers. A
    batch-wide flag would bury a single dead agent in a healthy majority, or
    turn the healthy majority into noise — and the degraded one is the agent
    the routing floor is currently unable to judge.
    """
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    bad = sorted(a.id for a in state.list_agents())[0]

    async def flaky(key: str, ttl_seconds: float, producer):
        # Matched on the agent id rather than the whole cache key: the
        # "repstate:" prefix is reputation_svc's private business.
        if key.endswith(bad):
            raise RuntimeError("rpc down")
        return {"sum_w": 9000 * 10 * STROOPS_PER_USDC, "weight": 10 * STROOPS_PER_USDC, "count": 4, "disputed": 0}

    monkeypatch.setattr(rcache, "get_or_set", flaky)

    body = client.get("/api/stellar/reputation").json()

    assert len(body["reputations"]) > 1, "a partial outage needs more than one agent to be partial"
    assert {aid for aid, info in body["reputations"].items() if info["degraded"]} == {bad}
    assert body["reputations"][bad]["source"] == "prior"
    for aid, info in body["reputations"].items():
        if aid != bad:
            assert info["source"] == "onchain"


# ── decompose stamping ──────────────────────────────────────────


def test_kit_decompose_stamps_reputation_fields(client):
    r = client.post("/api/orchestrator/decompose", json={"intent": "tetris game in html"})
    assert r.status_code == 200
    steps = r.json()["steps"]
    assert steps
    for step in steps:
        assert step["rep_bps"] == settings.reputation_prior_bps
        assert step["rep_source"] == "prior"


# ── routing floor ───────────────────────────────────────────────


def test_floor_omits_low_reputation_agent():
    seed_registry()
    agents = state.list_agents()
    reps = {a.id: _info(a.id, smoothed=8000, lower=8000) for a in agents}
    bad = agents[0].id
    reps[bad] = _info(bad, smoothed=3000, lower=1000)  # below the 5500 floor

    fragment = orchestrator_svc._registry_prompt_fragment(reps)

    assert f"id={bad} " not in fragment
    for a in agents[1:]:
        assert f"id={a.id} " in fragment


def test_floor_never_shrinks_below_three_agents():
    seed_registry()
    agents = state.list_agents()
    # Every agent fails the floor, with distinct smoothed scores.
    reps = {a.id: _info(a.id, smoothed=1000 + i * 10, lower=100) for i, a in enumerate(agents)}

    fragment = orchestrator_svc._registry_prompt_fragment(reps)

    listed = [ln for ln in fragment.splitlines() if ln.startswith("- id=")]
    assert len(listed) == 3
    top3 = sorted(agents, key=lambda a: reps[a.id].smoothed_bps, reverse=True)[:3]
    for a in top3:
        assert f"id={a.id} " in fragment


# ── settler rating submission ──────────────────────────────────


def test_submit_ratings_is_best_effort(monkeypatch):
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_signing_key", "SFAKEKEY")

    calls: list[tuple] = []

    async def fake_submit(agent_id, job_id, rating, weight, payer, kind="auto"):
        calls.append((agent_id, job_id, rating, weight, payer, kind))
        if agent_id == "agt_bad":
            raise RuntimeError("sequence collision")
        return {"hash": "deadbeefcafe0123", "status": "SUCCESS"}

    monkeypatch.setattr(sc, "submit_rating_async", fake_submit)

    steps = [
        PlanStep(
            agent_id="agt_ok",
            agent_name="w.ok",
            rationale="r",
            est_price_usdc=0.05,
            est_eta_seconds=1.0,
        ),
        PlanStep(
            agent_id="agt_bad",
            agent_name="w.bad",
            rationale="r",
            est_price_usdc=0.05,
            est_eta_seconds=1.0,
        ),
    ]
    plan = StoredPlan(
        id="pln_test",
        intent="test intent",
        plan=Plan(steps=steps),
        total_usdc=0.1,
        total_eta=2.0,
    )
    task_id = f"tsk_{secrets.token_hex(3)}"
    state.add_task(
        Task(
            id=task_id,
            intent="test intent",
            agents=2,
            spent=0.0,
            status="running",
            started="just now",
        )
    )
    # Context keys are worker names — w.ok delivered a clean artifact (95),
    # w.bad has no output (20) and its submit raises.
    context = {"w.ok": {"artifact": {"title": "x"}, "critic_violations": []}}

    # Must never raise, even with a failing submit in the middle.
    asyncio.run(
        execution_svc._submit_ratings(
            task_id,
            time.monotonic(),
            plan,
            context,
            payer="G" + "A" * 55,
            job_id=b"\x01" * 16,
        )
    )

    assert len(calls) == 2  # one submit per plan step
    assert all(c[1] == b"\x01" * 16 for c in calls)
    assert all(c[5] == "auto" for c in calls)
    assert calls[0][2] == 95 and calls[1][2] == 20

    lines = state.traces[task_id]
    proofs = [ln for ln in lines if ln.level == "proof"]
    errors = [ln for ln in lines if ln.level == "error"]
    assert len(proofs) == 1
    assert "w.ok rated 95/100" in proofs[0].msg
    assert len(errors) == 1
    assert "reputation submit failed for w.bad" in errors[0].msg


def test_submit_ratings_skips_when_not_configured(monkeypatch):
    # Hermetic default: no signing key → the gate must short-circuit
    # without touching the stellar client at all.
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_signing_key", "")

    def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("submit_rating called despite missing key")

    monkeypatch.setattr(sc, "submit_rating", boom)
    monkeypatch.setattr(sc, "submit_rating_async", boom)

    plan = StoredPlan(
        id="pln_skip",
        intent="x",
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id="agt_ok",
                    agent_name="w.ok",
                    rationale="r",
                    est_price_usdc=0.01,
                    est_eta_seconds=1.0,
                )
            ]
        ),
        total_usdc=0.01,
        total_eta=1.0,
    )
    asyncio.run(
        execution_svc._submit_ratings(
            "tsk_none",
            time.monotonic(),
            plan,
            {},
            payer="G" + "A" * 55,
            job_id=b"\x02" * 16,
        )
    )
    assert "tsk_none" not in state.traces
