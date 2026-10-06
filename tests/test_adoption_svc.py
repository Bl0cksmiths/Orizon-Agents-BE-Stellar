"""Adoption evidence — the tests that stop SOW §6.3's numbers flattering us.

What is pinned is not arithmetic but the two ways this metric could lie:

  - COUNTING OURSELVES. An agent owned by a declared team wallet, or by a key
    this deployment holds at runtime, is never external — whatever else is true
    of it — and is listed under `excluded` with the reason instead.
  - COUNTING IGNORANCE AS ZERO. A read that failed puts the agent in
    `unreadable_agents` and sets `degraded`; it never quietly contributes 0.

Hermetic: the seams are the ones the settlement tests use — `sc.simulate_read`
for the contract views, `settlement_svc.fetch_settlement` for per-agent charges
(one test drives the real one through `sc._server`), and the binding store.
No pytest-asyncio, so async entry points run under a bare `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from stellar_sdk import Keypair, scval
from test_settlement_svc import _auth_id, _charged_event, _FakeRpc, _job_id

from app.config import settings
from app.schemas import Agent
from app.services import adoption_svc, registry_sync, settlement_svc
from app.services.settlement_svc import SettlementEntry, SettlementEvidence
from app.state import state
from app.stellar import cache as rcache
from app.stellar import client as sc

ESCROW_ID = "CA" + "A" * 54
REGISTRY_ID = "CB" + "B" * 54
SAC_ID = "CC" + "C" * 54


def _key(n: int) -> Keypair:
    return Keypair.from_raw_ed25519_seed(bytes([n]) * 32)


def _g(n: int) -> str:
    return _key(n).public_key


# Outside operators: in neither the register nor the platform's keys.
EXT_A, EXT_B, EXT_C = _g(1), _g(2), _g(3)
BUYER = _g(4)  # an outside buyer
# The platform's runtime keys, as the chain and the config name them.
CONFIG_ADMIN = _g(10)
SETTLER = _g(11)
ESCROW_ADMIN = _g(12)
REGISTRY_ADMIN = _g(13)
DISPATCH = _g(14)
SIGNER = _key(15)

REGISTER = {w.address: w.role for w in adoption_svc.TEAM_REGISTER}
QA_OPERATOR = "GBWMD26IB6CMG3JO3HU7SD7ZJSTF4BIJ5JS77ANMLJ52M6FV6K3J7BQJ"
QA_BUYER = "GDJHP2I6NRCWYZTB3ZOXRE74V4M4EGXRYORGNPTGQ6BVNJNSSJO4PKXJ"

AT = "2026-09-16T08:57:00+00:00"
AT_UNIX = int(datetime.fromisoformat(AT).timestamp())


def _hash(n: int) -> str:
    return f"{n:064x}"


def _job(n: int) -> str:
    return f"{n:032x}"


class _Chain:
    """`sc.simulate_read`, answering the views the adoption report reads.

    Every answer may be an exception instance, which is raised instead.
    """

    def __init__(self) -> None:
        self.list_ids: Any = []
        self.owners: dict[str, Any] = {}
        self.settler: Any = SETTLER
        self.escrow_version: Any = 1
        # v1's escrow has no admin() at all: the chain's own answer, if asked.
        self.escrow_admin: Any = RuntimeError("simulate failed: HostError: Error(WasmVm, MissingValue)")
        self.registry_admin: Any = REGISTRY_ADMIN
        self.reads: list[tuple[str, str]] = []

    def __call__(self, contract_id: str, function_name: str, args: Any = None, source: Any = None, **_kw: Any) -> Any:
        self.reads.append((contract_id, function_name))
        if function_name == "list_ids":
            value = self.list_ids
        elif function_name == "owner_of":
            value = self.owners[str(scval.to_native(args[0]))]
        elif function_name == "settler":
            value = self.settler
        elif function_name == "admin" and contract_id == ESCROW_ID:
            value = self.escrow_admin
        elif function_name == "admin" and contract_id == REGISTRY_ID:
            value = self.registry_admin
        else:
            raise AssertionError(f"unexpected read: {contract_id}.{function_name}")
        if isinstance(value, BaseException):
            raise value
        return value


class _Store:
    """A binding store holding a record for `bound` ids; raises when `broken`."""

    def __init__(self, *bound: str, broken: bool = False) -> None:
        self.bound = set(bound)
        self.broken = broken

    async def get(self, agent_id: str) -> Any:
        if self.broken:
            raise RuntimeError("binding store unreachable")
        return object() if agent_id in self.bound else None


def _entry(
    job: int,
    *,
    payer: str = BUYER,
    exclusion: settlement_svc.Exclusion | None = None,
    amount: int = 100_000,
    tx: str | None | int = None,
    at: str | None = AT,
) -> SettlementEntry:
    return SettlementEntry(
        job_id=_job(job),
        auth_id=_job(1000 + job),
        amount_stroops=amount,
        ledger=1_000 + job,
        tx_hash=_hash(job) if tx is None else (None if tx == 0 else _hash(int(tx))),
        at=at,
        payer=payer,
        self_payment=exclusion is not None,
        exclusion=exclusion,
    )


def _evidence(
    agent_id: str, *entries: SettlementEntry, truncated: bool = False, window_days: float = 7.0
) -> SettlementEvidence:
    return SettlementEvidence(
        agent_id=agent_id,
        asset="native",
        window_days=window_days,
        scanned_ledgers=120_960,
        entries=list(entries),
        total_stroops=sum(e.amount_stroops for e in entries if not e.self_payment),
        self_payment_stroops=sum(e.amount_stroops for e in entries if e.self_payment),
        truncated=truncated,
        unavailable=None,
    )


def _unavailable(agent_id: str) -> SettlementEvidence:
    return settlement_svc._unavailable(agent_id, "soroban rpc unreachable")


class _World:
    """Everything the report reads, set up per test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.chain = _Chain()
        self.settlements: dict[str, Any] = {}
        self.settlement_calls: list[str] = []
        self.store = _Store()
        monkeypatch.setattr(sc, "simulate_read", self.chain)
        monkeypatch.setattr(sc, "cached_escrow_version", lambda _id: None)
        monkeypatch.setattr(sc, "escrow_version", self._escrow_version)
        monkeypatch.setattr(settlement_svc, "fetch_settlement", self._fetch)
        monkeypatch.setattr(adoption_svc, "get_binding_store", lambda: self.store)
        monkeypatch.setattr(adoption_svc, "dispatch_signer_address", lambda: DISPATCH)

    def _escrow_version(self, contract_id: str) -> int:
        assert contract_id == ESCROW_ID
        if isinstance(self.chain.escrow_version, BaseException):
            raise self.chain.escrow_version
        return int(self.chain.escrow_version)

    async def _fetch(self, agent_id: str) -> SettlementEvidence:
        self.settlement_calls.append(agent_id)
        value = self.settlements.get(agent_id) or _evidence(agent_id)
        if isinstance(value, BaseException):
            raise value
        return value

    def agent(
        self, agent_id: str, owner: str | None, *entries: SettlementEntry, active: bool = True, listed: bool = True
    ) -> None:
        state.add_agent(
            Agent(
                id=agent_id,
                name=f"{agent_id} name",
                skills=["x"],
                price=0.01,
                rep=3.5,
                status="online" if active else "offline",
                runs=0,
                owner=owner,
                source="onchain",
            )
        )
        if listed:
            self.chain.list_ids.append(agent_id)
        if entries:
            self.settlements[agent_id] = _evidence(agent_id, *entries)

    def report(self) -> adoption_svc.AdoptionReport:
        rcache.clear()
        return asyncio.run(adoption_svc.build_report())


@pytest.fixture()
def world(hermetic_settings: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    saved_agents = dict(state.agents)
    state.agents.clear()
    rcache.clear()
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY_ID)
    monkeypatch.setattr(settings, "stellar_payment_escrow", ESCROW_ID)
    monkeypatch.setattr(settings, "stellar_asset_sac", SAC_ID)
    monkeypatch.setattr(settings, "stellar_admin_address", CONFIG_ADMIN)
    yield _World(monkeypatch)
    state.agents.clear()
    state.agents.update(saved_agents)
    rcache.clear()


def _totals(report: adoption_svc.AdoptionReport) -> tuple[int, int, int]:
    t = report.totals
    return t.external_agents, t.unique_operator_wallets, t.settled_external_workflows


def _excluded(report: adoption_svc.AdoptionReport) -> dict[str, tuple[str, str, list[str]]]:
    return {e.owner: (e.reason, e.role, e.agent_ids) for e in report.excluded}


# ── who counts ──────────────────────────────────────────────────────────────
def test_an_external_owner_counts_with_its_settled_workflow_linked(world: _World) -> None:
    world.store = _Store("ext_a")
    world.agent("ext_a", EXT_A, _entry(1, amount=1_234_567))

    report = world.report()

    assert _totals(report) == (1, 1, 1)
    assert report.degraded is False and report.unreadable_agents == [] and report.excluded == []
    (operator,) = report.operators
    assert operator.owner == EXT_A
    assert operator.owner_explorer == f"https://stellar.expert/explorer/testnet/account/{EXT_A}"
    (agent,) = operator.agents
    assert (agent.agent_id, agent.name, agent.active, agent.bound) == ("ext_a", "ext_a name", True, True)
    (workflow,) = agent.settled_workflows
    assert workflow.model_dump() == {
        "job_id_hex": _job(1),
        "tx_hash": _hash(1),
        "explorer": f"https://stellar.expert/explorer/testnet/tx/{_hash(1)}",
        "amount_usdc": 0.1234567,
        "payer": BUYER,
        "payer_team_role": None,
        "settled_at": AT_UNIX,
    }


@pytest.mark.parametrize("owner", sorted(REGISTER))
def test_a_team_owner_never_counts_however_real_its_agent_looks(world: _World, owner: str) -> None:
    """Bound, active, and paid by an outside buyer with a linked transaction —
    everything an external agent would have — and still ours."""
    world.store = _Store("ours")
    world.agent("ours", owner, _entry(1), _entry(2, payer=EXT_B))

    report = world.report()

    assert _totals(report) == (0, 0, 0)
    assert report.operators == []
    assert _excluded(report) == {owner: ("team_wallet", REGISTER[owner], ["ours"])}
    assert world.settlement_calls == []  # never even asked whether it was paid


def test_team_and_external_owners_mixed_count_only_the_external(world: _World) -> None:
    world.agent("ext_a1", EXT_A, _entry(1))
    world.agent("ext_a2", EXT_A, _entry(2))
    world.agent("ext_b1", EXT_B)
    world.agent("qa_1", QA_OPERATOR, _entry(3))
    world.agent("qa_2", QA_OPERATOR, _entry(4))
    world.agent("probe", "GBI2I3WLMP2Q6L26G7CBKRPP5WJ6G3GGYJHWALOJ7D6EBRGL5OZAADBH", _entry(5))

    report = world.report()

    assert _totals(report) == (3, 2, 2)
    assert [(op.owner, [a.agent_id for a in op.agents]) for op in report.operators] == sorted(
        [(EXT_A, ["ext_a1", "ext_a2"]), (EXT_B, ["ext_b1"])]
    )
    assert {o: ids for o, (_r, _role, ids) in _excluded(report).items()} == {
        QA_OPERATOR: ["qa_1", "qa_2"],
        "GBI2I3WLMP2Q6L26G7CBKRPP5WJ6G3GGYJHWALOJ7D6EBRGL5OZAADBH": ["probe"],
    }


@pytest.mark.parametrize(
    ("owner", "role"),
    [
        (CONFIG_ADMIN, "deployment admin"),
        (SIGNER.public_key, "deployment signing key"),
        (DISPATCH, "dispatch signer"),
        (SETTLER, "escrow settler"),
        (ESCROW_ADMIN, "escrow admin"),
        (REGISTRY_ADMIN, "registry admin"),
    ],
)
def test_a_runtime_platform_key_is_excluded_without_being_in_the_register(
    world: _World, monkeypatch: pytest.MonkeyPatch, owner: str, role: str
) -> None:
    monkeypatch.setattr(settings, "stellar_signing_key", SIGNER.secret)
    world.chain.escrow_version = 2  # a v2 escrow, which has the view
    world.chain.escrow_admin = ESCROW_ADMIN
    world.agent("platform_owned", owner, _entry(1))
    world.agent("ext_a", EXT_A)
    assert owner not in REGISTER

    report = world.report()

    assert _totals(report) == (1, 1, 0)
    assert _excluded(report) == {owner: ("platform_key", role, ["platform_owned"])}
    assert report.degraded is False


def test_the_register_outranks_a_runtime_key_for_the_reason(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared wallet that is ALSO a live platform key reads as the declared
    one: that is the entry carrying evidence a reviewer can check."""
    admin = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
    monkeypatch.setattr(settings, "stellar_admin_address", admin)
    world.chain.registry_admin = admin
    world.agent("Testing_Agent", admin)

    assert _excluded(world.report()) == {admin: ("team_wallet", REGISTER[admin], ["Testing_Agent"])}


def test_seeded_and_ownerless_agents_are_never_operators(world: _World) -> None:
    state.add_agent(Agent(id="agt_01h8", name="seed", skills=[], price=0.1, rep=4.0, status="online", runs=0))
    world.agent("agt_squat", EXT_A)  # an on-chain id in the seeded namespace
    world.agent("no_owner", None)

    report = world.report()

    assert _totals(report) == (0, 0, 0)
    assert report.unreadable_agents == ["no_owner"]
    assert report.degraded is True


# ── what counts as a settled workflow ───────────────────────────────────────
def test_one_job_paying_two_external_agents_is_one_workflow(world: _World) -> None:
    world.agent("ext_a", EXT_A, _entry(7, tx=70), _entry(8))
    world.agent("ext_b", EXT_B, _entry(7, tx=71))

    report = world.report()

    assert _totals(report) == (2, 2, 2)
    listed = [w.job_id_hex for op in report.operators for a in op.agents for w in a.settled_workflows]
    assert sorted(listed) == sorted([_job(7), _job(8), _job(7)])


@pytest.mark.parametrize("exclusion", ["owner", "settler", "payer_unreadable", "settler_unreadable"])
def test_a_charge_settlement_excludes_is_not_a_settled_workflow(
    world: _World, exclusion: settlement_svc.Exclusion
) -> None:
    world.agent("ext_a", EXT_A, _entry(1, payer=EXT_A, exclusion=exclusion), _entry(2))

    report = world.report()

    assert _totals(report) == (1, 1, 1)
    assert [w.job_id_hex for w in report.operators[0].agents[0].settled_workflows] == [_job(2)]
    assert report.degraded is False


def test_a_team_buyer_paying_an_external_operator_counts_and_is_flagged(world: _World) -> None:
    world.agent("ext_a", EXT_A, _entry(1, payer=QA_BUYER), _entry(2, payer=DISPATCH), _entry(3))

    report = world.report()

    assert _totals(report) == (1, 1, 3)
    flags = {w.job_id_hex: w.payer_team_role for w in report.operators[0].agents[0].settled_workflows}
    assert flags == {_job(1): REGISTER[QA_BUYER], _job(2): "dispatch signer", _job(3): None}


def test_the_real_settlement_scan_excludes_what_it_excludes(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through `settlement_svc`: the agent's own owner paying and
    the platform's settler paying are not revenue, a third party paying is."""
    monkeypatch.setattr(settlement_svc, "fetch_settlement", _REAL_FETCH)
    world.agent("ext_agent", EXT_A)
    world.chain.owners["ext_agent"] = EXT_A
    payers = {_auth_id(1).hex(): EXT_A, _auth_id(2).hex(): SETTLER, _auth_id(3).hex(): BUYER}
    rpc = _FakeRpc(
        events=[
            _charged_event(1_000_010, _auth_id(1), _job_id(1), 100_000),
            _charged_event(1_000_020, _auth_id(2), _job_id(2), 100_000),
            _charged_event(1_000_030, _auth_id(3), _job_id(3), 100_000),
        ]
    )
    chain = world.chain

    def read(contract_id: str, function_name: str, args: Any = None, source: Any = None, **kw: Any) -> Any:
        if function_name == "authorization":
            return {"payer": payers[bytes(scval.to_native(args[0])).hex()]}
        if function_name == "name":
            return "native"
        return chain(contract_id, function_name, args, source, **kw)

    monkeypatch.setattr(sc, "simulate_read", read)
    monkeypatch.setattr(sc, "_server", lambda **_kw: rpc)

    report = world.report()

    assert _totals(report) == (1, 1, 1)
    (workflow,) = report.operators[0].agents[0].settled_workflows
    assert (workflow.job_id_hex, workflow.payer) == (_job_id(3).hex(), BUYER)
    assert report.degraded is False
    # The window is the one the scan measured, not a constant.
    scanned = asyncio.run(settlement_svc.fetch_settlement("ext_agent"))
    assert scanned.window_days > 0
    assert report.window_days == scanned.window_days


_REAL_FETCH = settlement_svc.fetch_settlement


# ── how far back it looked ──────────────────────────────────────────────────
def test_the_window_is_the_scans_own_measured_span(world: _World) -> None:
    world.agent("ext_a", EXT_A)
    world.settlements["ext_a"] = _evidence("ext_a", _entry(1), window_days=6.482)

    report = world.report()

    assert report.window_days == 6.482


def test_differing_windows_report_the_smallest_so_the_claim_holds_for_all(world: _World) -> None:
    world.agent("ext_a", EXT_A)
    world.agent("ext_b", EXT_B)
    world.agent("ext_c", EXT_C)
    world.settlements["ext_a"] = _evidence("ext_a", _entry(1), window_days=7.0)
    world.settlements["ext_b"] = _evidence("ext_b", window_days=6.9)
    # A truncated scan still ran; the span it did cover is the honest bound.
    world.settlements["ext_c"] = _evidence("ext_c", truncated=True, window_days=2.5)

    report = world.report()

    assert report.window_days == 2.5


@pytest.mark.parametrize(
    "failure",
    [_unavailable("ext_b"), RuntimeError("cache layer"), TimeoutError()],
    ids=["unavailable", "raised", "timed_out"],
)
def test_a_scan_that_did_not_run_does_not_shrink_the_window(world: _World, failure: Any) -> None:
    """It has no window to report; its absence is `unreadable_agents`' job."""
    world.agent("ext_a", EXT_A, _entry(1))
    world.agent("ext_b", EXT_B)
    world.settlements["ext_b"] = failure

    report = world.report()

    assert report.window_days == 7.0
    assert report.unreadable_agents == ["ext_b"]


def test_no_scan_at_all_is_a_zero_window(world: _World) -> None:
    world.agent("ext_a", EXT_A)
    world.settlements["ext_a"] = _unavailable("ext_a")
    world.agent("ours", QA_OPERATOR)

    report = world.report()

    assert report.window_days == 0.0
    assert world.settlement_calls == ["ext_a"]


# ── ignorance is never zero ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "failure",
    [_unavailable("ext_b"), RuntimeError("cache layer"), TimeoutError()],
    ids=["unavailable", "raised", "timed_out"],
)
def test_a_failed_settlement_read_is_unreadable_never_zero(world: _World, failure: Any) -> None:
    world.agent("ext_a", EXT_A, _entry(1))
    world.agent("ext_b", EXT_B)
    world.settlements["ext_b"] = failure

    report = world.report()

    assert report.degraded is True
    assert report.unreadable_agents == ["ext_b"]
    # Still an external agent: its registration is a fact, its earnings are not known.
    assert _totals(report) == (2, 2, 1)


def test_a_truncated_scan_counts_what_it_saw_and_admits_it(world: _World) -> None:
    world.agent("ext_a", EXT_A)
    world.settlements["ext_a"] = _evidence("ext_a", _entry(1), truncated=True)

    report = world.report()

    assert _totals(report) == (1, 1, 1)
    assert (report.degraded, report.unreadable_agents) == (True, ["ext_a"])


def test_a_verified_charge_with_no_usable_hash_is_not_counted(world: _World) -> None:
    world.agent("ext_a", EXT_A, _entry(1, tx=0), _entry(2))

    report = world.report()

    assert _totals(report) == (1, 1, 1)
    assert (report.degraded, report.unreadable_agents) == (True, ["ext_a"])


def test_an_on_chain_agent_the_mirror_lacks_is_unreadable_unless_ours(world: _World) -> None:
    """The registry lists ids the mirror does not hold (a cold process, a
    refused price): ours are excluded by name, anyone else's is unreadable."""
    world.agent("ext_a", EXT_A)
    world.chain.list_ids += ["orizon_batch", "stranger", "gone", "agt_05x7"]
    world.chain.owners.update(
        {
            "orizon_batch": "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
            "stranger": EXT_C,
            "gone": ConnectionError("rpc down"),
        }
    )

    report = world.report()

    assert _totals(report) == (1, 1, 0)
    assert report.unreadable_agents == ["gone", "stranger"]
    assert report.degraded is True
    assert _excluded(report)["GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"][2] == ["orizon_batch"]


@pytest.mark.parametrize(
    "break_it",
    [
        lambda chain: setattr(chain, "list_ids", ConnectionError("rpc down")),
        lambda chain: setattr(chain, "settler", ConnectionError("rpc down")),
        lambda chain: setattr(chain, "registry_admin", ConnectionError("rpc down")),
    ],
    ids=["list_ids", "escrow_settler", "registry_admin"],
)
def test_an_unreadable_registry_or_platform_key_degrades_the_report(world: _World, break_it: Any) -> None:
    world.agent("ext_a", EXT_A, _entry(1))
    break_it(world.chain)

    report = world.report()

    assert report.degraded is True
    assert _totals(report) == (1, 1, 1)


def test_a_v1_escrow_is_never_asked_for_an_admin_it_does_not_have(world: _World) -> None:
    world.agent("ext_a", EXT_A)

    report = world.report()

    assert (ESCROW_ID, "admin") not in world.chain.reads
    assert report.degraded is False


@pytest.mark.parametrize("version", [2, ConnectionError("rpc down")], ids=["v2", "version_unreadable"])
def test_an_escrow_admin_read_that_fails_does_not_degrade(world: _World, version: Any) -> None:
    """Best effort on purpose: the admin it names is the deployment admin,
    which is excluded from configuration already."""
    world.chain.escrow_version = version
    world.agent("ext_a", EXT_A)

    report = world.report()

    assert (ESCROW_ID, "admin") in world.chain.reads
    assert report.degraded is False


def test_no_registry_configured_is_degraded_not_a_clean_zero(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "stellar_agent_registry", "")

    report = world.report()

    assert _totals(report) == (0, 0, 0)
    assert report.degraded is True


def test_an_unreadable_binding_store_leaves_bound_unknown(world: _World) -> None:
    world.store = _Store(broken=True)
    world.agent("ext_a", EXT_A, active=False)

    agent = world.report().operators[0].agents[0]

    assert (agent.bound, agent.active) == (None, False)


# ── the targets ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n", [0, 1, 2, 3, 4])
def test_met_flips_exactly_at_each_target(world: _World, n: int) -> None:
    """n external owners, each with one agent and one distinct settled job."""
    for i in range(n):
        world.agent(f"ext_{i}", _g(40 + i), _entry(i + 1))

    report = world.report()

    assert _totals(report) == (n, n, n)
    assert report.targets.model_dump() == {
        "external_agents": 2,
        "unique_operator_wallets": 2,
        "settled_external_workflows": 3,
    }
    assert report.met.model_dump() == {
        "external_agents": n >= 2,
        "unique_operator_wallets": n >= 2,
        "settled_external_workflows": n >= 3,
    }


def test_two_agents_under_one_owner_are_one_operator_wallet(world: _World) -> None:
    world.agent("ext_a1", EXT_A)
    world.agent("ext_a2", EXT_A)

    report = world.report()

    assert _totals(report) == (2, 1, 0)
    assert report.met.external_agents is True and report.met.unique_operator_wallets is False


# ── the cache ───────────────────────────────────────────────────────────────
def test_concurrent_callers_share_one_computation(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0
    real_build = adoption_svc.build_report

    async def slow_build() -> adoption_svc.AdoptionReport:
        nonlocal builds
        builds += 1
        await asyncio.sleep(0.05)
        return await real_build()

    monkeypatch.setattr(adoption_svc, "build_report", slow_build)
    monkeypatch.setattr(registry_sync, "status", lambda: registry_sync.SyncStatus(synced=True))
    world.agent("ext_a", EXT_A, _entry(1))
    rcache.clear()

    async def many() -> list[adoption_svc.AdoptionReport]:
        cell = adoption_svc.report_cell
        first = await asyncio.gather(*(cell.get(wait_seconds=None) for _ in range(8)))
        again = await adoption_svc.report_snapshot()  # inside the refresh interval: a hit
        return [snap.value for snap in (*first, again) if snap is not None]

    reports = asyncio.run(many())

    assert builds == 1
    assert len(reports) == 9
    assert world.settlement_calls == ["ext_a"]
    assert all(r is reports[0] for r in reports)


def test_the_settlement_reads_fan_out_with_bounded_concurrency(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    in_flight = peak = 0

    async def fetch(agent_id: str) -> SettlementEvidence:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _evidence(agent_id)

    monkeypatch.setattr(settlement_svc, "fetch_settlement", fetch)
    for i in range(10):
        world.agent(f"ext_{i}", _g(60 + i))

    world.report()

    assert peak == adoption_svc.READ_CONCURRENCY


# ── probe bounds ────────────────────────────────────────────────────────────
def test_a_stalled_binding_probe_costs_that_agent_its_answer_not_the_build(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Hung:
        async def get(self, agent_id: str) -> Any:
            await asyncio.sleep(30)

    monkeypatch.setattr(adoption_svc, "PROBE_TIMEOUT_SECONDS", 0.05)
    world.store = _Hung()  # type: ignore[assignment]
    world.agent("ext_a", EXT_A, _entry(1))

    started = time.perf_counter()
    report = world.report()

    assert time.perf_counter() - started < 2.0
    assert report.operators[0].agents[0].bound is None
    assert _totals(report) == (1, 1, 1)


def test_a_stalled_owner_probe_leaves_the_agent_unreadable(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def hung(agent_id: str) -> str | None:
        await asyncio.sleep(30)
        return None

    monkeypatch.setattr(adoption_svc, "PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(adoption_svc.external_binding, "resolve_owner", hung)
    world.agent("ext_a", EXT_A, _entry(1))
    world.chain.list_ids.append("unmirrored_one")

    started = time.perf_counter()
    report = world.report()

    assert time.perf_counter() - started < 2.0
    assert report.unreadable_agents == ["unmirrored_one"]
    assert report.degraded is True
