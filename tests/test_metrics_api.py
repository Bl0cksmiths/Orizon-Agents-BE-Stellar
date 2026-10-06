"""GET /api/metrics/overview reports only measured values (ADR 0013).

What is pinned is the two ways the dashboard used to lie and must not again:

  - INVENTING NUMBERS. No baseline is added to a count, no constant stands in
    for a rate, and there is no `DEMO_` symbol left in `app/` to bring one back.
  - DRESSING UP IGNORANCE. A source that cannot be read gives null (or []) and
    sets `degraded` — never a fallback value that looks measured.

Plus the rules each number is computed by: external agents by the adoption
report's own owner rule, settled workflows from the durable settlement store,
trust from on-chain evidence only, and the cache that keeps polling cheap.

Hermetic. The seams are the platform-key read (`adoption_svc._platform_keys`),
the binding set (`binding_registry`), the settlement store (`get_dispute_store`
as the router imports it) and `reputation_svc.fetch_reps`.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from stellar_sdk import Keypair
from test_dispute_store import a_settlement

from app.main import app
from app.routers import metrics as metrics_router
from app.schemas import Agent, OverviewMetrics, Task
from app.security import EXEMPT_PATHS, RateLimitMiddleware
from app.services import (
    adoption_svc,
    binding_registry,
    charge_window,
    registry_sync,
    reputation_svc,
    settlement_svc,
    snapshots,
)
from app.services.dispute_store import SECONDS_PER_DAY, InMemoryDisputeStore
from app.services.reputation_svc import RepInfo
from app.state import state
from app.stellar import cache as rcache

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _g(n: int) -> str:
    return Keypair.from_raw_ed25519_seed(bytes([n]) * 32).public_key


TEAM = adoption_svc.TEAM_REGISTER[0].address  # a declared team wallet
PLATFORM = _g(40)  # a key this deployment holds at runtime
EXT_A, EXT_B, EXT_C = _g(41), _g(42), _g(43)  # outside operators


# ── fixtures ──────────────────────────────────────────────────────────────
def _clear_rate_limiter() -> None:
    """Drop the app-wide sliding-window hit table. The limiter instance lives
    for the whole pytest process, so this file's extra requests would otherwise
    eat into the shared per-minute budget and 429 later test files."""
    node = getattr(app, "middleware_stack", None)
    while node is not None and not isinstance(node, RateLimitMiddleware):
        node = getattr(node, "app", None)
    if node is not None:
        node._hits.clear()


@pytest.fixture(autouse=True)
def budget_neutral_rate_limit() -> Iterator[None]:
    _clear_rate_limiter()
    yield
    _clear_rate_limiter()


@pytest.fixture(autouse=True)
def clean_tasks() -> Iterator[None]:
    saved_tasks = dict(state.tasks)
    saved_order = list(state.task_order)
    state.tasks.clear()
    state.task_order.clear()
    yield
    state.tasks.clear()
    state.task_order.clear()
    state.tasks.update(saved_tasks)
    state.task_order.extend(saved_order)


@pytest.fixture(autouse=True)
def fresh_cache_and_notes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An empty cache and unobserved log notes, so each test computes afresh."""
    rcache.clear()
    for name in ("_external_note", "_bound_note", "_workflows_note", "_trust_note"):
        monkeypatch.setattr(metrics_router, name, metrics_router._SourceNote(name))
    yield
    rcache.clear()


@pytest.fixture(autouse=True)
def platform_keys(monkeypatch: pytest.MonkeyPatch) -> adoption_svc._PlatformKeys:
    """The deployment's runtime keys, without a chain read: PLATFORM is ours."""
    keys = adoption_svc._PlatformKeys(roles={PLATFORM: "dispatch signer"})

    async def fake() -> adoption_svc._PlatformKeys:
        return keys

    monkeypatch.setattr(adoption_svc, "_platform_keys", fake)
    return keys


@pytest.fixture(autouse=True)
def bindings(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """A loaded, empty bound set; tests add ids to the returned set."""
    bound: set[str] = set()
    monkeypatch.setattr(binding_registry, "_bound_ids", bound)
    monkeypatch.setattr(binding_registry, "_loaded", True)
    return bound


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryDisputeStore:
    s = InMemoryDisputeStore()
    monkeypatch.setattr(metrics_router, "get_dispute_store", lambda: s)
    return s


@pytest.fixture(autouse=True)
def unrated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every agent on the flat prior, read cleanly: nothing rated yet."""
    _patch_reps(monkeypatch, lambda a: _info(a, 7000, "prior"))


@pytest.fixture(autouse=True)
def registry_synced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mirror that has finished its first full pass. The latch is process-wide
    and set by whichever pass ran last, so it is pinned rather than inherited;
    a test of the unsynced overview pins `status` itself."""
    monkeypatch.setattr(
        registry_sync, "_status", registry_sync.SyncStatus(synced=True, agents=0, last_full_sync_at=1.0)
    )


def _unsynced(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mirror still filling after a restart. `status` itself is replaced, so a
    pass the TestClient's lifespan runs cannot complete it under the test."""
    monkeypatch.setattr(registry_sync, "status", lambda: registry_sync.SyncStatus(syncing=True))


@pytest.fixture
def registry() -> Iterator[Callable[..., None]]:
    """Replace the registry's agents for one test; restored afterwards."""
    saved = dict(state.agents)

    def use(*agents: Agent) -> None:
        state.agents.clear()
        state.agents.update({a.id: a for a in agents})

    yield use
    state.agents.clear()
    state.agents.update(saved)


# ── helpers ───────────────────────────────────────────────────────────────
def _agent(
    agent_id: str,
    *,
    source: str = "onchain",
    owner: str | None = None,
    status: str = "online",
    skills: tuple[str, ...] = ("code",),
) -> Agent:
    return Agent(
        id=agent_id,
        name=agent_id,
        skills=list(skills),
        price=0.01,
        rep=4.9,
        status=status,
        runs=0,
        owner=owner,
        source=source,
    )


def _seeded(agent_id: str, **kw: object) -> Agent:
    return _agent(agent_id, source="seeded", **kw)


def _info(agent_id: str, smoothed_bps: int, source: str, *, degraded: bool = False) -> RepInfo:
    onchain = source == "onchain"
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed_bps,
        lower_bound_bps=0,
        avg_bps=smoothed_bps if onchain else 0,
        count=1 if onchain else 0,
        weight=10_000_000 if onchain else 0,
        disputed=0,
        dispute_rate_bps=0,
        source=source,
        degraded=degraded,
    )


def _patch_reps(monkeypatch: pytest.MonkeyPatch, builder: Callable[[str], RepInfo]) -> None:
    async def fake(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        return {a: builder(a) for a in agent_ids}

    monkeypatch.setattr("app.services.reputation_svc.fetch_reps", fake)


def _overview(client) -> dict:
    """A fresh computation: the cache and the snapshot are cleared first so
    state set up by the test is what is measured."""
    rcache.clear()
    metrics_router.overview_cell.reset()
    r = client.get("/api/metrics/overview")
    assert r.status_code == 200, r.text
    return r.json()


def _add_tasks(statuses: list[str]) -> None:
    for i, status in enumerate(statuses):
        state.add_task(Task(id=f"mt{i}", intent="demo", agents=1, spent=0.0, status=status, started="just now"))


# ── the shape ─────────────────────────────────────────────────────────────
def test_overview_response_shape_is_stable(client) -> None:
    body = _overview(client)
    assert set(body) == {
        "generated_at",
        "agents",
        "operators",
        "workflows",
        "tasks",
        "trust",
        "skills",
        "registry_synced",
        "degraded",
    }
    assert set(body["agents"]) == {"registered", "onchain", "seeded", "external", "bound", "online"}
    assert set(body["operators"]) == {"external_wallets"}
    assert set(body["workflows"]) == {"settled", "series"}
    assert len(body["workflows"]["series"]) == metrics_router.SERIES_DAYS == 14
    assert all(set(day) == {"date", "settled"} for day in body["workflows"]["series"])
    assert set(body["tasks"]) == {"recent", "complete", "failed", "completion_rate"}
    assert set(body["trust"]) == {"avg", "rated_agents"}
    assert all(set(s) == {"name", "agents", "pct"} for s in body["skills"])
    assert isinstance(body["generated_at"], float)
    assert body["registry_synced"] is True
    assert body["degraded"] is False


def test_no_fake_fields_survive(client) -> None:
    body = _overview(client)
    for gone in ("agents_online", "tasks_per_sec", "avg_completion", "avg_trust", "throughput"):
        assert gone not in body
    assert all("tone" not in s for s in body["skills"])


def test_no_demo_symbol_anywhere_in_app() -> None:
    """The baselines were `DEMO_*` constants; none may come back under that name."""
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}:{n}"
        for path in sorted(APP_DIR.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json", ".md", ".txt", ".yaml", ".toml"}
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if re.search(r"DEMO_", line)
    ]
    assert offenders == []


def test_the_route_keeps_the_app_rate_limit() -> None:
    assert "/api/metrics/overview" not in EXEMPT_PATHS


# ── agents and operators ──────────────────────────────────────────────────
def test_seeded_and_onchain_are_counted_by_source(client, registry) -> None:
    registry(
        _seeded("agt_a"), _seeded("agt_b"), _seeded("agt_c"), _agent("op1", owner=EXT_A), _agent("op2", owner=EXT_A)
    )
    agents = _overview(client)["agents"]
    assert agents["registered"] == 5
    assert agents["seeded"] == 3
    assert agents["onchain"] == 2


def test_registered_is_exactly_the_marketplace_list(client, registry) -> None:
    registry(_seeded("agt_a"), _agent("op1", owner=EXT_A, status="offline"))
    assert _overview(client)["agents"]["registered"] == len(client.get("/api/agents").json()) == 2


def test_external_excludes_team_and_platform_wallets(client, registry) -> None:
    registry(
        _agent("ours_team", owner=TEAM),  # in app/data/team_wallets.json
        _agent("ours_key", owner=PLATFORM),  # a key this deployment holds
        _agent("ext1", owner=EXT_A),
        _agent("ext2", owner=EXT_B),
        _seeded("agt_x"),
    )
    body = _overview(client)
    assert body["agents"]["onchain"] == 4
    assert body["agents"]["external"] == 2
    assert body["operators"]["external_wallets"] == 2
    assert body["degraded"] is False


def test_external_wallets_are_distinct_owners(client, registry) -> None:
    registry(
        _agent("ext1", owner=EXT_A),
        _agent("ext2", owner=EXT_A),
        _agent("ext3", owner=EXT_A),
        _agent("ext4", owner=EXT_B),
    )
    body = _overview(client)
    assert body["agents"]["external"] == 4
    assert body["operators"]["external_wallets"] == 2


def test_external_is_decided_by_the_adoption_rule_itself(client, registry, monkeypatch) -> None:
    """Not a copy of it: a rule that says every owner is ours leaves nothing external."""
    registry(_agent("ext1", owner=EXT_A), _agent("ext2", owner=EXT_B))

    class Everyone(adoption_svc.OwnerRule):
        def classify(self, owner: str) -> tuple[adoption_svc.ExclusionReason, str] | None:
            return "team_wallet", "test"

    async def rule() -> adoption_svc.OwnerRule:
        return Everyone(register={}, platform=adoption_svc._PlatformKeys())

    monkeypatch.setattr(adoption_svc, "owner_rule", rule)
    body = _overview(client)
    assert body["agents"]["external"] == 0
    assert body["operators"]["external_wallets"] == 0


def test_a_seeded_prefix_on_chain_is_never_external(client, registry) -> None:
    """The adoption report's mirror rule: `agt_` is the platform's namespace."""
    registry(_agent("agt_imposter", owner=EXT_A), _agent("ext1", owner=EXT_B))
    body = _overview(client)
    assert body["agents"]["onchain"] == 2
    assert body["agents"]["external"] == 1


def test_an_onchain_agent_with_no_owner_is_not_counted_and_degrades(client, registry) -> None:
    registry(_agent("ext1", owner=EXT_A), _agent("mystery", owner=None))
    body = _overview(client)
    assert body["agents"]["external"] == 1
    assert body["degraded"] is True


def test_unreadable_platform_keys_degrade_the_external_count(client, registry, platform_keys) -> None:
    platform_keys.unreadable.append("escrow settler()")
    registry(_agent("ext1", owner=EXT_A))
    body = _overview(client)
    assert body["agents"]["external"] == 1
    assert body["degraded"] is True


def test_a_failing_owner_rule_is_null_not_zero(client, registry, monkeypatch) -> None:
    registry(_agent("ext1", owner=EXT_A))

    async def broken() -> adoption_svc.OwnerRule:
        raise RuntimeError("boom")

    monkeypatch.setattr(adoption_svc, "owner_rule", broken)
    body = _overview(client)
    assert body["agents"]["external"] is None
    assert body["operators"]["external_wallets"] is None
    assert body["degraded"] is True


def test_bound_counts_onchain_bindings_and_online_counts_status(client, registry, bindings) -> None:
    registry(
        _agent("op1", owner=EXT_A, status="online"),
        _agent("op2", owner=EXT_B, status="idle"),
        _agent("op3", owner=EXT_C, status="offline"),
        _seeded("agt_a", status="online"),
        _seeded("agt_b", status="idle"),
    )
    # A seeded id in the set is not an endpoint binding: seeded agents run in-process.
    bindings.update({"op1", "op2", "agt_a"})
    agents = _overview(client)["agents"]
    assert agents["bound"] == 2
    assert agents["online"] == 2


def test_bound_is_null_while_the_binding_set_is_unloaded(client, registry, monkeypatch) -> None:
    monkeypatch.setattr(binding_registry, "_loaded", False)
    registry(_agent("op1", owner=EXT_A))
    body = _overview(client)
    assert body["agents"]["bound"] is None
    assert body["degraded"] is True


# ── the registry mirror ───────────────────────────────────────────────────
def test_a_mirror_still_filling_serves_its_counts_as_partial(client, registry, monkeypatch) -> None:
    """After a restart the mirror holds a prefix of the registry. Its counts are
    real and are served, but they are not the registry's: the flag says so."""
    _unsynced(monkeypatch)
    registry(_seeded("agt_a"), _agent("ext1", owner=EXT_A), _agent("ext2", owner=EXT_B))
    body = _overview(client)
    assert body["registry_synced"] is False
    assert body["degraded"] is True
    assert body["agents"]["registered"] == 3  # still returned, not nulled
    assert body["agents"]["onchain"] == 2
    assert body["agents"]["external"] == 2
    assert body["operators"]["external_wallets"] == 2


def test_a_synced_mirror_is_not_degraded_by_the_registry(client, registry) -> None:
    registry(_seeded("agt_a"), _agent("ext1", owner=EXT_A))
    body = _overview(client)
    assert body["registry_synced"] is True
    assert body["degraded"] is False


def test_the_overview_never_runs_the_adoption_settlement_scan(registry, monkeypatch) -> None:
    """External agents and wallets need owners, not charges. The settlement
    scans behind /api/ecosystem/adoption take 13-40 s; any of them reached from
    here would make the dashboard that slow, so each one raises if touched."""

    def scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("the overview reached a settlement scan")

    async def ascan(*args: object, **kwargs: object) -> None:
        scan()

    for owner, name, fake in (
        (settlement_svc, "fetch_settlement", ascan),
        (settlement_svc, "_scan_sync", scan),
        (charge_window, "fetch_settlements", ascan),
        (adoption_svc, "_window", ascan),
        (adoption_svc, "_unmirrored", ascan),
        (adoption_svc, "build_report", ascan),
        (adoption_svc, "report_snapshot", ascan),
    ):
        monkeypatch.setattr(owner, name, fake)
    registry(_agent("ext1", owner=EXT_A), _agent("ext2", owner=EXT_B), _agent("ours", owner=TEAM))

    started = time.perf_counter()
    overview = asyncio.run(metrics_router.build_overview())

    assert time.perf_counter() - started < 2.0
    assert overview.agents.external == 2
    assert overview.operators.external_wallets == 2
    assert overview.degraded is False


# ── workflows ─────────────────────────────────────────────────────────────
NOW = 1_790_000_000.0  # 2026-09-21 14:13:20 UTC
TODAY = int(NOW // SECONDS_PER_DAY)


def _job(n: int) -> str:
    return f"{n:064x}"


async def _settle(store: InMemoryDisputeStore, n: int, day: int, offset: float = 3_600.0) -> None:
    await store.record_settlement(a_settlement(job_id_hex=_job(n), settled_at=day * SECONDS_PER_DAY + offset))


def test_series_is_fourteen_utc_days_oldest_first_with_empty_days(store) -> None:
    async def go() -> metrics_router._Workflows:
        await _settle(store, 1, TODAY)
        await _settle(store, 2, TODAY - 2)
        await _settle(store, 3, TODAY - 2, offset=SECONDS_PER_DAY - 1)  # 23:59:59, same day
        await _settle(store, 4, TODAY - 13)  # the oldest day in the series
        await _settle(store, 5, TODAY - 14)  # outside the series, still settled
        return await metrics_router._workflows(NOW)

    result = asyncio.run(go())
    assert result.settled == 5
    assert result.degraded is False
    dates = [d.date for d in result.series]
    assert dates[0] == "2026-09-08"
    assert dates[-1] == "2026-09-21" == datetime.fromtimestamp(NOW, UTC).date().isoformat()
    assert dates == sorted(dates) and len(set(dates)) == 14
    counts = [d.settled for d in result.series]
    assert counts == [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 1]


def test_settled_comes_from_the_settlement_store(client, store) -> None:
    async def go() -> None:
        today = int(time.time() // SECONDS_PER_DAY)
        for n in range(3):
            await _settle(store, n, today - 20)  # all payers, all time: not only the series
        await _settle(store, 9, today, offset=0.0)

    asyncio.run(go())
    workflows = _overview(client)["workflows"]
    assert workflows["settled"] == 4
    assert sum(d["settled"] for d in workflows["series"]) == 1


def test_an_unreadable_store_is_null_and_empty_and_degraded(client, monkeypatch) -> None:
    class Down(InMemoryDisputeStore):
        async def count_settled_by_day(self) -> dict[int, int]:
            raise ConnectionError("database unreachable")

    monkeypatch.setattr(metrics_router, "get_dispute_store", Down)
    body = _overview(client)
    assert body["workflows"] == {"settled": None, "series": []}
    assert body["degraded"] is True


def test_a_hung_store_is_cut_off_at_the_budget(monkeypatch) -> None:
    class Hung(InMemoryDisputeStore):
        async def count_settled_by_day(self) -> dict[int, int]:
            await asyncio.sleep(10)
            return {}

    monkeypatch.setattr(metrics_router, "get_dispute_store", Hung)
    monkeypatch.setattr(metrics_router, "SETTLEMENT_READ_BUDGET_SECONDS", 0.05)
    started = time.monotonic()
    result = asyncio.run(metrics_router._workflows(NOW))
    assert time.monotonic() - started < 2
    assert (result.settled, result.series, result.degraded) == (None, [], True)


# ── tasks ─────────────────────────────────────────────────────────────────
def test_completion_rate_is_null_with_no_terminal_task(client) -> None:
    _add_tasks(["running", "pending"])
    assert _overview(client)["tasks"] == {"recent": 2, "complete": 0, "failed": 0, "completion_rate": None}


def test_completion_rate_is_null_with_no_tasks(client) -> None:
    assert _overview(client)["tasks"]["completion_rate"] is None


def test_completion_counts_terminal_tasks_only(client) -> None:
    _add_tasks(["complete", "failed", "running", "running", "pending", "complete", "complete", "failed"])
    tasks = _overview(client)["tasks"]
    # 3 complete of 5 decided; the 3 undecided tasks must not drag the rate.
    assert tasks == {"recent": 8, "complete": 3, "failed": 2, "completion_rate": 0.6}


# ── trust ─────────────────────────────────────────────────────────────────
def test_trust_averages_only_onchain_evidence(client, registry, monkeypatch) -> None:
    registry(_agent("r1", owner=EXT_A), _agent("r2", owner=EXT_B), _seeded("agt_a"))
    _patch_reps(
        monkeypatch,
        lambda a: {"r1": _info(a, 8000, "onchain"), "r2": _info(a, 9000, "onchain")}.get(a, _info(a, 7000, "prior")),
    )
    # (8000 + 9000) / 2 bps → 4.25 on the 0–5 scale; the prior is not evidence.
    assert _overview(client)["trust"] == {"avg": 4.25, "rated_agents": 2}


def test_no_onchain_evidence_is_null_not_a_fallback(client, registry) -> None:
    registry(_seeded("agt_a"), _seeded("agt_b"))
    body = _overview(client)
    # Not the seeded registry average (4.9), not the 3.5 prior, not 4.86.
    assert body["trust"] == {"avg": None, "rated_agents": 0}
    # Nothing rated yet is a measured state, not a failure.
    assert body["degraded"] is False


def test_a_degraded_reputation_read_degrades(client, registry, monkeypatch) -> None:
    registry(_seeded("agt_a"), _agent("r1", owner=EXT_A))
    _patch_reps(monkeypatch, lambda a: _info(a, 7000, "prior", degraded=True))
    body = _overview(client)
    assert body["trust"] == {"avg": None, "rated_agents": 0}
    assert body["degraded"] is True


def test_a_raising_reputation_read_is_null(client, registry, monkeypatch) -> None:
    registry(_seeded("agt_a"))

    async def boom(agent_ids: list[str], timeout_seconds: float | None = None) -> dict[str, RepInfo]:
        raise RuntimeError("bug")

    monkeypatch.setattr("app.services.reputation_svc.fetch_reps", boom)
    body = _overview(client)
    assert body["trust"] == {"avg": None, "rated_agents": None}
    assert body["degraded"] is True


# ── skills ────────────────────────────────────────────────────────────────
def test_skills_are_the_top_five_then_other_summing_to_100(client, registry) -> None:
    registry(
        _agent("a1", owner=EXT_A, skills=("code", "react")),
        _agent("a2", owner=EXT_A, skills=("code", "seo")),
        _agent("a3", owner=EXT_A, skills=("code", "Research ")),  # normalised
        _seeded("agt_1", skills=("research", "seo")),
        _seeded("agt_2", skills=("design", "ui")),
        _seeded("agt_3", skills=("ops", "ci", "ui")),
    )
    skills = _overview(client)["skills"]
    # code 3; research 2, seo 2, ui 2 (alphabetical on ties); then ci, design,
    # ops, react on 1 each — "ci" takes the fifth place.
    assert [s["name"] for s in skills] == ["code", "research", "seo", "ui", "ci", "other"]
    assert [s["agents"] for s in skills] == [3, 2, 2, 2, 1, 3]  # other: agt_2, agt_3, a1
    assert sum(s["pct"] for s in skills) == 100
    # 13 skill tags: code 3/13, the named 2/13 or 1/13, other 3/13 — the two
    # points left after flooring go to the largest remainders (ci, then research).
    assert [s["pct"] for s in skills] == [23, 16, 15, 15, 8, 23]


def test_no_other_row_with_five_skills_or_fewer(client, registry) -> None:
    registry(_agent("a1", owner=EXT_A, skills=("code",)), _agent("a2", owner=EXT_B, skills=("code", "seo")))
    skills = _overview(client)["skills"]
    assert skills == [{"name": "code", "agents": 2, "pct": 67}, {"name": "seo", "agents": 1, "pct": 33}]


def test_an_empty_registry_has_no_skill_mix(client, registry) -> None:
    registry()
    body = _overview(client)
    assert body["skills"] == []
    assert body["agents"]["registered"] == 0


def test_a_skill_named_other_folds_into_the_other_row(client, registry) -> None:
    registry(
        _agent("a1", owner=EXT_A, skills=("other", "a", "b", "c", "d", "e", "f")),
        _agent("a2", owner=EXT_A, skills=("other",)),
    )
    skills = _overview(client)["skills"]
    assert [s["name"] for s in skills] == ["a", "b", "c", "d", "e", "other"]
    assert skills[-1]["agents"] == 2
    assert sum(s["pct"] for s in skills) == 100


@pytest.mark.parametrize(
    "weights, expected",
    [
        ([1, 1, 1], [34, 33, 33]),
        ([1, 1, 1, 1, 1, 1, 1], [15, 15, 14, 14, 14, 14, 14]),
        ([7], [100]),
        ([3, 1], [75, 25]),
    ],
)
def test_largest_remainder_always_sums_to_100(weights: list[int], expected: list[int]) -> None:
    assert metrics_router._largest_remainder(weights) == expected


# ── caching ───────────────────────────────────────────────────────────────
def _counting_build(monkeypatch: pytest.MonkeyPatch, delay: float = 0.0) -> list[int]:
    calls: list[int] = []
    real = metrics_router.build_overview

    async def counted() -> OverviewMetrics:
        calls.append(1)
        await asyncio.sleep(delay)
        return await real()

    monkeypatch.setattr(metrics_router, "build_overview", counted)
    return calls


async def _refresh_behind() -> None:
    """Wait for the rebuild a stale read started behind itself."""
    task = metrics_router.overview_cell._live_task()
    assert task is not None, "a stale read must start a rebuild"
    await task


def test_the_cache_ttl_is_short() -> None:
    assert 5.0 <= metrics_router.OVERVIEW_CACHE_TTL_SECONDS <= 30.0


def test_polls_within_the_ttl_share_one_computation(client, registry, monkeypatch) -> None:
    calls = _counting_build(monkeypatch)
    registry(_seeded("agt_a"))
    first = client.get("/api/metrics/overview").json()
    registry(_seeded("agt_a"), _seeded("agt_b"))  # changes inside the TTL are not seen
    second = client.get("/api/metrics/overview").json()
    assert len(calls) == 1
    assert second == first
    assert second["agents"]["registered"] == 1


def test_an_expired_overview_is_served_once_while_it_refreshes(registry, monkeypatch) -> None:
    """Past the TTL the next poll gets the last overview at once and starts one
    refresh behind it; the poll after that sees the refreshed numbers."""
    calls = _counting_build(monkeypatch)
    monkeypatch.setattr(metrics_router.overview_cell, "fresh_seconds", 0.2)
    registry(_seeded("agt_a"))

    async def go() -> tuple[OverviewMetrics, ...]:
        first = await metrics_router.fetch_overview()
        cached = await metrics_router.fetch_overview()
        registry(_seeded("agt_a"), _seeded("agt_b"))
        await asyncio.sleep(0.3)
        stale = await metrics_router.fetch_overview()
        await _refresh_behind()
        return first, cached, stale, await metrics_router.fetch_overview()

    first, cached, stale, refreshed = asyncio.run(go())
    assert len(calls) == 2
    assert cached is first
    assert stale is first
    assert refreshed.agents.registered == 2
    assert refreshed.generated_at > first.generated_at


def test_concurrent_polls_are_single_flight(registry, monkeypatch) -> None:
    calls = _counting_build(monkeypatch, delay=0.05)
    registry(_seeded("agt_a"))

    async def go() -> list[OverviewMetrics]:
        return list(await asyncio.gather(*(metrics_router.fetch_overview() for _ in range(8))))

    results = asyncio.run(go())
    assert len(calls) == 1
    assert all(r is results[0] for r in results)


def test_a_slow_build_never_delays_a_poll_with_a_recent_overview(registry, monkeypatch) -> None:
    """The latency path: a rebuild that runs to the reputation deadline must not
    be what a dashboard poll waits on once there is an overview to serve."""
    calls = _counting_build(monkeypatch, delay=0.5)
    monkeypatch.setattr(metrics_router.overview_cell, "fresh_seconds", 0.05)
    registry(_seeded("agt_a"))

    async def go() -> tuple[OverviewMetrics, OverviewMetrics, float]:
        first = await metrics_router.fetch_overview()  # nothing to serve: this one waits
        await asyncio.sleep(0.1)
        started = time.perf_counter()
        polled = await metrics_router.fetch_overview()
        elapsed = time.perf_counter() - started
        await _refresh_behind()
        return first, polled, elapsed

    first, polled, elapsed = asyncio.run(go())
    assert polled is first
    assert elapsed < 0.1
    assert len(calls) == 2  # and the refresh did run, behind the poll


def test_an_overview_too_old_to_serve_waits_for_a_fresh_one(registry, monkeypatch) -> None:
    calls = _counting_build(monkeypatch)
    monkeypatch.setattr(metrics_router.overview_cell, "fresh_seconds", 0.05)
    monkeypatch.setattr(metrics_router.overview_cell, "max_serve_seconds", 0.1)
    registry(_seeded("agt_a"))

    async def go() -> tuple[OverviewMetrics, OverviewMetrics]:
        first = await metrics_router.fetch_overview()
        registry(_seeded("agt_a"), _seeded("agt_b"))
        await asyncio.sleep(0.2)
        return first, await metrics_router.fetch_overview()

    first, later = asyncio.run(go())
    assert len(calls) == 2
    assert later is not first
    assert later.agents.registered == 2


def test_the_first_full_pass_replaces_a_partial_overview_at_once(registry, monkeypatch) -> None:
    """Inside the TTL, and not served stale: the cached counts were a prefix,
    and the registry total is the number its readers are waiting for."""
    calls = _counting_build(monkeypatch)
    registry(_seeded("agt_a"))
    _unsynced(monkeypatch)

    async def go() -> tuple[OverviewMetrics, OverviewMetrics]:
        partial = await metrics_router.fetch_overview()
        registry(_seeded("agt_a"), _agent("ext1", owner=EXT_A))
        monkeypatch.setattr(registry_sync, "status", lambda: registry_sync.SyncStatus(synced=True, agents=2))
        return partial, await metrics_router.fetch_overview()

    partial, full = asyncio.run(go())
    assert partial.registry_synced is False
    assert full.registry_synced is True
    assert full.agents.registered == 2
    assert len(calls) == 2


# ── HTTP caching ──────────────────────────────────────────────────────────
def test_the_overview_carries_validators_and_a_shared_cache_lifetime(client, registry) -> None:
    registry(_seeded("agt_a"))
    r = client.get("/api/metrics/overview")
    assert r.status_code == 200
    assert r.headers["etag"].startswith('W/"')
    assert r.headers["last-modified"].endswith("GMT")
    assert r.headers["cache-control"].startswith("public, max-age=")
    assert "s-maxage=" in r.headers["cache-control"]
    assert "stale-while-revalidate=60" in r.headers["cache-control"]
    assert int(r.headers["x-snapshot-age"]) >= 0
    assert OverviewMetrics.model_validate(r.json()).agents.registered == 1


def test_a_current_etag_is_answered_304_without_a_body(client, registry) -> None:
    registry(_seeded("agt_a"))
    first = client.get("/api/metrics/overview")
    again = client.get("/api/metrics/overview", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == first.headers["etag"]


def test_a_gzip_client_gets_the_same_overview(client, registry) -> None:
    registry(_seeded("agt_a"))
    plain = client.get("/api/metrics/overview", headers={"Accept-Encoding": "identity"})
    zipped = client.get("/api/metrics/overview", headers={"Accept-Encoding": "gzip"})
    assert zipped.headers["content-encoding"] == "gzip"
    assert zipped.json() == plain.json()


def test_a_partial_overview_is_never_held_by_a_shared_cache(client, registry, monkeypatch) -> None:
    registry(_seeded("agt_a"))
    _unsynced(monkeypatch)
    r = client.get("/api/metrics/overview")
    assert r.json()["registry_synced"] is False
    assert r.headers["cache-control"] == "no-cache"


def test_a_failed_rebuild_keeps_serving_the_last_overview(registry, monkeypatch) -> None:
    """Stale on failure: an overview that cannot be rebuilt is not replaced by
    an error while there is one to serve, and the failure is recorded."""
    registry(_seeded("agt_a"))
    monkeypatch.setattr(metrics_router.overview_cell, "fresh_seconds", 0.01)

    async def broken() -> OverviewMetrics:
        raise RuntimeError("store exploded")

    async def go() -> tuple[OverviewMetrics, OverviewMetrics]:
        first = await metrics_router.fetch_overview()
        monkeypatch.setattr(metrics_router, "build_overview", broken)
        await asyncio.sleep(0.05)
        served = await metrics_router.fetch_overview()
        await _refresh_behind()
        return first, served

    first, served = asyncio.run(go())
    assert served is first
    assert metrics_router.overview_cell.status().last_error == "RuntimeError: store exploded"


def test_no_overview_at_all_is_503_never_a_fabricated_one(registry, monkeypatch) -> None:
    async def broken() -> OverviewMetrics:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(metrics_router, "build_overview", broken)
    r = TestClient(app).get("/api/metrics/overview")
    assert r.status_code == 503
    assert r.json()["detail"] == "overview_unavailable"


def test_the_overview_is_kept_warm_and_rebuilt_on_a_registry_pass() -> None:
    schedule = next(s for s in snapshots._schedules if s.cell is metrics_router.overview_cell)
    assert schedule.every_seconds <= 60
    assert schedule.fingerprint is not None
    assert schedule.fingerprint() == registry_sync.status().last_full_sync_at


def test_a_warm_overview_answers_well_inside_the_latency_budget(client, registry, monkeypatch) -> None:
    """The point of the snapshot: once one exists, a poll is a memory read,
    however slow the build behind it is."""
    registry(*(_agent(f"ext{i}", owner=EXT_A) for i in range(600)))
    _counting_build(monkeypatch, delay=1.0)
    client.get("/api/metrics/overview")  # the first one waits for its build
    started = time.perf_counter()
    for _ in range(10):
        assert client.get("/api/metrics/overview").status_code == 200
    assert (time.perf_counter() - started) / 10 < 0.2


def test_a_landed_rating_expires_the_overview_for_the_next_poll(registry, monkeypatch) -> None:
    """Trust is an average of ratings: one landing is served once more from
    the snapshot, which rebuilds behind that poll and shows it."""
    registry(_agent("ext1", owner=EXT_A))
    calls = _counting_build(monkeypatch)

    async def go() -> tuple[OverviewMetrics, OverviewMetrics, OverviewMetrics]:
        first = await metrics_router.fetch_overview()
        _patch_reps(monkeypatch, lambda a: _info(a, 9000, "onchain"))
        reputation_svc.invalidate_rep("ext1")
        served = await metrics_router.fetch_overview()
        await _refresh_behind()
        return first, served, await metrics_router.fetch_overview()

    first, served, rebuilt = asyncio.run(go())
    assert served is first
    assert first.trust.avg is None
    assert rebuilt.trust.avg == 4.5
    assert len(calls) == 2
