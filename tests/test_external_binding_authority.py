"""The authority half of endpoint binding — `resolve_owner` (ADR 0003 D2).

This read IS the authorization: a binding submits nothing and no contract
re-checks it afterwards, so unlike every other registry read in this repo it
must fail CLOSED. These tests pin the three outcomes apart — an owner, a
genuinely absent agent, and a chain that could not be read — plus the cache
namespace that keeps a public, pollable key out of the decision.

The suite is hermetic: `sc.simulate_read` is monkeypatched in every test that
reaches it, and nothing here touches the network. There is no pytest-asyncio, so
async entry points are driven with a bare `asyncio.run` per the repo idiom.
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.services import external_binding as eb
from app.services.external_binding import OWNER_CACHE_TTL_SECONDS, OwnerLookupError, resolve_owner
from app.stellar import cache as rcache
from app.stellar import client as sc

REGISTRY_ID = "CA" + "A" * 54
OTHER_REGISTRY_ID = "CB" + "B" * 54
OWNER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
ATTACKER = "GDUKMGUGDZQK6YHYA5Z6AY2G4XDSZPSZ3SW5UN3ARVMO6QSRDWP5YLEX"

# Exactly what sc.simulate_read raises when the chain ANSWERED and the host
# function failed — owner_of panics on an id the registry does not hold.
CONTRACT_ERROR = RuntimeError("simulate failed: HostError: Error(Contract, #1)")


@pytest.fixture(autouse=True)
def configured_registry(hermetic_settings: Any) -> Any:
    """Give resolve_owner a registry id, and an empty read cache per test.

    conftest forces `stellar_agent_registry` to "" so the hermetic suite can
    never reach testnet; these tests put an id back so resolve_owner gets past
    its own configuration gate, and depend on that fixture explicitly so the
    ordering (and its restore) is not left to chance.
    """
    rcache.clear()
    settings.stellar_agent_registry = REGISTRY_ID
    yield settings
    rcache.clear()


def _reads(result: Any = OWNER) -> tuple[Any, list[tuple[str, str]]]:
    """A fake simulate_read returning `result`, plus the call log it appends to.
    A BaseException instance is raised instead of returned."""
    calls: list[tuple[str, str]] = []

    def fake_read(contract_id: str, function_name: str, args: Any = None, source: Any = None) -> Any:
        calls.append((contract_id, function_name))
        if isinstance(result, BaseException):
            raise result
        return result

    return fake_read, calls


def test_resolve_owner_returns_the_on_chain_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_read, calls = _reads()
    monkeypatch.setattr(sc, "simulate_read", fake_read)

    assert asyncio.run(resolve_owner("ext_agent")) == OWNER
    # one single-agent owner_of read against the configured registry — never a
    # list_ids scan (ADR 0003 D2)
    assert calls == [(REGISTRY_ID, "owner_of")]


def test_an_agent_that_is_not_on_chain_resolves_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chain answered and owner_of failed: "no such agent", a 404 — which
    is a different fact from "the chain is unreachable", a 503."""
    fake_read, _ = _reads(CONTRACT_ERROR)
    monkeypatch.setattr(sc, "simulate_read", fake_read)

    assert asyncio.run(resolve_owner("ext_missing")) is None


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("rpc unreachable"),
        TimeoutError(),  # carries no message — the _describe bare-type branch
        RuntimeError("no source address; set STELLAR_ADMIN_ADDRESS"),
        ValueError("garbage from the rpc"),
    ],
)
def test_an_unreadable_chain_raises_instead_of_returning_none(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    """FAIL CLOSED. No answer from the chain can ever become "unknown owner,
    allow", and it must not be laundered into the None that means 404 either —
    a caller that saw None would report the agent as absent.
    """
    fake_read, _ = _reads(failure)
    monkeypatch.setattr(sc, "simulate_read", fake_read)

    with pytest.raises(OwnerLookupError):
        asyncio.run(resolve_owner("ext_agent"))


def test_a_read_without_an_address_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful simulate that produced no address is not an answer we can
    authorize against, so it is refused rather than guessed at."""
    for empty in (None, "", 42):
        rcache.clear()
        fake_read, _ = _reads(empty)
        monkeypatch.setattr(sc, "simulate_read", fake_read)
        with pytest.raises(OwnerLookupError):
            asyncio.run(resolve_owner("ext_agent"))


def test_an_unconfigured_registry_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No registry means nothing to authorize against — refuse, do not shrug.

    This is also the hermetic suite's own configuration, so the default posture
    of this function with no chain available is "deny".
    """
    settings.stellar_agent_registry = ""

    def must_not_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("resolve_owner read the chain with no registry configured")

    monkeypatch.setattr(sc, "simulate_read", must_not_read)
    with pytest.raises(OwnerLookupError):
        asyncio.run(resolve_owner("ext_agent"))


def test_the_registry_id_is_read_live_not_through_the_lru_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """sc.contract_ids() is lru_cached and would pin whatever id (or blank) it
    saw first for the life of the process — registry_sync.py:18-22 records the
    same reasoning. Reconfiguration must be visible on the very next read."""
    fake_read, calls = _reads()
    monkeypatch.setattr(sc, "simulate_read", fake_read)

    asyncio.run(resolve_owner("ext_live_1"))
    settings.stellar_agent_registry = OTHER_REGISTRY_ID
    asyncio.run(resolve_owner("ext_live_2"))

    assert [contract_id for contract_id, _ in calls] == [REGISTRY_ID, OTHER_REGISTRY_ID]


def test_the_owner_read_is_cached_under_its_own_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """`agentowner:{id}`, never `agent:{id}`.

    `agent:{agent_id}` is fail-OPEN and publicly pollable through
    GET /api/stellar/agent/{id}, so sharing it would let an attacker pre-warm
    the very cache this authorization decision reads from.
    """
    fake_read, calls = _reads()
    monkeypatch.setattr(sc, "simulate_read", fake_read)

    # a poisoned public read key, as an attacker could warm it
    poison = (time.monotonic() + 60, {"id": "ext_agent", "owner": ATTACKER})
    rcache._store["agent:ext_agent"] = poison

    assert asyncio.run(resolve_owner("ext_agent")) == OWNER
    assert "agentowner:ext_agent" in rcache._store
    assert rcache._store["agent:ext_agent"] == poison  # read, but never consulted
    assert len(calls) == 1
    # short by design: within the TTL a FORMER owner could still bind, so this
    # is 3 s rather than the 15 s the marketplace reads use
    assert OWNER_CACHE_TTL_SECONDS == 3.0


def test_repeat_lookups_inside_the_ttl_collapse_to_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_read, calls = _reads()
    monkeypatch.setattr(sc, "simulate_read", fake_read)

    async def twice() -> tuple[str | None, str | None]:
        return await resolve_owner("ext_cached"), await resolve_owner("ext_cached")

    assert asyncio.run(twice()) == (OWNER, OWNER)
    assert len(calls) == 1


def test_the_module_never_reads_the_cached_marketplace_owner() -> None:
    """AC-1, enforced at the source.

    `state.agents[agent_id].owner` is already in memory and free, which is
    exactly why it is a trap: up to 15 s stale by design, stale INDEFINITELY
    through an RPC outage (the sync loop fails open and never dies), and None
    for seeded agents. Using it would be an authorization bug, so the module
    must not so much as import the marketplace state — which is asserted
    structurally rather than by grepping the text, because the module documents
    the trap by name and prose must not be mistaken for a reference.
    """
    import app.state

    # nothing in the module's namespace can reach the cached catalog
    assert "state" not in vars(eb)
    assert not any(value is app.state.state for value in vars(eb).values())

    # and no late/function-local import smuggles it in either
    tree = ast.parse(Path(eb.__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name.split(".")[-1] == "state" for name in imported), imported
