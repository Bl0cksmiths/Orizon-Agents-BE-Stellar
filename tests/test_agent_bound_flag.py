"""`Agent.bound` — what the marketplace tells a buyer about an agent's endpoint.

Story 3.05 needs binding state for agents the viewer does NOT own, for a whole
registry at once. The client's per-agent hook cannot answer that (one HTTP
request each, capped, and only for the connected wallet), so the answer rides
on the agent list the page already fetches.

What these tests pin is the meaning of the three values, because the field is
worthless — worse than absent — if any of them is guessed:

  - `None` for a seeded agent, ALWAYS. It runs on a worker inside this process
    and has no endpoint to bind, so `False` would report a defect where there
    is none.
  - `True`/`False` for an on-chain agent, from the bound set the planner uses.
  - `None` again for an on-chain agent when that set has never been loaded,
    because an unread set is ignorance, not an answer about someone's agent.

And the trap underneath all of it: `is_dispatchable` is True for every seeded
agent by design (they have local workers), so answering `bound` through it
would report the entire catalog as bound. One test holds that line directly.

Hermetic: conftest blanks `stellar_agent_registry`, so the 1.02 sync loop that
lifespan starts no-ops and nothing here reaches the network. The bound set is
loaded through `refresh_bound_ids` with a fake store — the same public route
tests/test_external_agent_routability.py uses — and emptied again afterwards.
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.registry import get_worker
from app.schemas import Agent
from app.seed import seed_registry
from app.services import binding_registry
from app.state import state

BOUND = "ext_bound1"
UNBOUND = "ext_unbound1"

# A seeded agent that definitely HAS a local worker, so the is_dispatchable
# trap is armed rather than hypothetical.
SEEDED_WITH_WORKER = "agt_01h8"


class _FakeStore:
    """Stands in for a BindingStore. Only `list_agent_ids` is exercised —
    `refresh_bound_ids` is the one public way to load the bound set."""

    def __init__(self, *agent_ids: str) -> None:
        self._ids = frozenset(agent_ids)

    async def list_agent_ids(self) -> frozenset[str]:
        return self._ids


class _UnreadableStore:
    """A store that cannot answer — the boot-time failure `refresh_bound_ids`
    swallows so an unreachable database never stops the service."""

    async def list_agent_ids(self) -> frozenset[str]:
        raise RuntimeError("binding store unreachable")


def _load_bound(monkeypatch, *agent_ids: str) -> None:
    """Seed (or clear) the bound set through the registry's public surface."""
    monkeypatch.setattr(binding_registry, "get_binding_store", lambda: _FakeStore(*agent_ids))
    asyncio.run(binding_registry.refresh_bound_ids())


def _go_cold(monkeypatch) -> None:
    """Put the registry in the state a process is in before its startup load
    has succeeded: nothing known, and `_loaded` still False."""
    _load_bound(monkeypatch)
    monkeypatch.setattr(binding_registry, "get_binding_store", _UnreadableStore)
    monkeypatch.setattr(binding_registry, "_loaded", False)


def _external(agent_id: str) -> Agent:
    """An indexed on-chain agent: in the marketplace, no local worker."""
    return Agent(
        id=agent_id,
        name=f"indexed.{agent_id}",
        skills=["code", "html"],
        price=0.02,
        rep=4.99,
        status="online",
        runs=0,
        real=False,
        owner="GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
        source="onchain",
    )


@pytest.fixture()
def marketplace(client, monkeypatch):
    """The catalog plus two indexed on-chain agents, one bound and one not."""
    seed_registry()
    state.add_agent(_external(BOUND))
    state.add_agent(_external(UNBOUND))
    _load_bound(monkeypatch, BOUND)
    yield client
    state.agents.pop(BOUND, None)
    state.agents.pop(UNBOUND, None)
    _load_bound(monkeypatch)


def _listed(client) -> dict[str, dict]:
    r = client.get("/api/agents")
    assert r.status_code == 200
    return {row["id"]: row for row in r.json()}


def test_a_seeded_agent_reports_bound_as_none(marketplace):
    rows = _listed(marketplace)
    seeded = [row for row in rows.values() if row["source"] == "seeded"]
    assert seeded, "the catalog must be seeded"
    for row in seeded:
        # Present and null — not absent. An omitted key is indistinguishable
        # from an old backend on the wire, and the client would have to guess.
        assert "bound" in row
        assert row["bound"] is None


def test_a_seeded_agent_with_a_local_worker_is_not_reported_as_bound(marketplace):
    # The trap, stated as an assertion: this agent IS dispatchable — a local
    # worker executes its steps — and dispatchable is not bound. A `bound`
    # derived from is_dispatchable would report the whole catalog as bound.
    assert get_worker(SEEDED_WITH_WORKER) is not None
    assert binding_registry.is_dispatchable(SEEDED_WITH_WORKER) is True

    assert _listed(marketplace)[SEEDED_WITH_WORKER]["bound"] is None


def test_an_onchain_agent_with_a_binding_reports_true(marketplace):
    assert _listed(marketplace)[BOUND]["bound"] is True


def test_an_onchain_agent_without_a_binding_reports_false(marketplace):
    # No worker and no binding: nothing could serve a call to it, and unlike a
    # seeded agent that IS a defect a buyer needs to see.
    assert get_worker(UNBOUND) is None
    assert _listed(marketplace)[UNBOUND]["bound"] is False


def test_the_single_agent_route_agrees_with_the_list_route(marketplace):
    # Two routes, one helper — but a buyer reaches the detail view from the
    # list, so a disagreement between them is the badge changing under them.
    for agent_id, row in _listed(marketplace).items():
        r = marketplace.get(f"/api/agents/{agent_id}")
        assert r.status_code == 200
        assert r.json()["bound"] is row["bound"], agent_id


def test_an_unread_bound_set_reports_unknown_rather_than_unbound(marketplace, monkeypatch):
    _go_cold(monkeypatch)
    # The startup load fails and is swallowed, exactly as it is in production
    # against a database that is briefly down.
    asyncio.run(binding_registry.refresh_bound_ids())
    assert binding_registry._loaded is False

    rows = _listed(marketplace)
    # Both on-chain agents: we know nothing about either, and saying "unbound"
    # would be publishing our own ignorance as a fact about their service.
    assert rows[BOUND]["bound"] is None
    assert rows[UNBOUND]["bound"] is None
    # Seeded agents are unaffected — their None never depended on the store.
    assert rows[SEEDED_WITH_WORKER]["bound"] is None


def test_a_bind_this_process_served_is_reported_even_before_a_load(marketplace, monkeypatch):
    # First-hand knowledge beats a missing startup read: `note_bound` is called
    # on every accepted bind, so an operator who just bound must see it, not a
    # null that reads as "we are not sure you exist".
    _go_cold(monkeypatch)
    binding_registry.note_bound(BOUND)

    rows = _listed(marketplace)
    assert rows[BOUND]["bound"] is True
    assert rows[UNBOUND]["bound"] is None
