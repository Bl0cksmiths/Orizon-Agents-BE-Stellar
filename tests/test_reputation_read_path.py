"""The reputation read, driven through the REAL cache with only the RPC faked.

Every older degradation test replaced `cache.get_or_set` wholesale, so the
properties that decide what the routing floor actually sees — which reads a
deadline keeps, whether an aborted read still lands, what a failed read leaves
in the cache — were never exercised together. Here `simulate_read` is the only
stand-in, and a slow read is a thread parked on an Event the test releases, so
nothing waits on wall-clock luck.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.services import reputation_svc as rep
from app.stellar import cache as rcache
from app.stellar import client as sc

USDC = rep.STROOPS_PER_USDC
LEDGER = "CFAKELEDGER"
# Heavy negative evidence: 20 USDC of 10/100 ratings, well under the floor.
BAD = {"sum_w": 1000 * 20 * USDC, "weight": 20 * USDC, "count": 40, "disputed": 0}
GOOD = {"sum_w": 9000 * 10 * USDC, "weight": 10 * USDC, "count": 4, "disputed": 0}


class Chain:
    """A scriptable ReputationLedger. `hold(agent)` parks that agent's reads
    until `release(agent)`; everything else answers at once."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.reads: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self.gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def hold(self, agent: str) -> None:
        self.gates[agent] = threading.Event()

    def release(self, agent: str) -> None:
        self.gates[agent].set()

    def simulate_read(self, contract_id: str, method: str, args: list[Any], **kwargs: Any) -> Any:
        assert (contract_id, method) == (LEDGER, "rep_state")
        agent = args[0]
        with self._lock:
            self.reads.append(agent)
            self.kwargs.append(kwargs)
        gate = self.gates.get(agent)
        if gate is not None:
            assert gate.wait(10), f"{agent}'s read was never released"
        value = self.state.get(agent, {"sum_w": 0, "weight": 0, "count": 0, "disputed": 0})
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture()
def chain(monkeypatch) -> Chain:
    fake = Chain()
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(reputation_ledger=LEDGER))
    monkeypatch.setattr(sc, "sym", lambda s: s)
    monkeypatch.setattr(sc, "simulate_read", fake.simulate_read)
    rcache.clear()
    yield fake
    for gate in fake.gates.values():
        gate.set()
    rcache.clear()


def test_a_reputation_read_is_one_round_trip(chain):
    """rep_state is a view: the read asks the client to skip load_account."""
    asyncio.run(rep.fetch_reps(["agt_01h8"]))

    assert chain.kwargs == [{"load_source": False}]
