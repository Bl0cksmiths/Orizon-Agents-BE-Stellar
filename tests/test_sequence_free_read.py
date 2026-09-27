"""A view read can skip `load_account` — and only when it asks to.

Every Soroban read used to be two sequential round trips: `load_account` for
the source's sequence number, then `simulateTransaction`. A simulation never
checks that number, so for a pure view call the first hop is latency and
nothing else — and the reputation batch, 23 agents deep behind a 2.5 s
deadline, spent half its budget on it. `load_source=False` builds the envelope
from the source at sequence 0 instead. The default is unchanged, because an
envelope that is signed and sent needs its real sequence.
"""

from __future__ import annotations

import pytest
from stellar_sdk import Account

from app.config import settings
from app.stellar import client as sc

# Public testnet ids: the envelope is really built, so both must be well formed.
_LEDGER = "CDCSOBEVZUPQZV5GV4D6KYHZCLNGW2KXY74RUHSZ3EZUXF34DPW422ZT"
_SOURCE = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"


class _Simulation:
    error = None
    results: list = []


class _Server:
    def __init__(self) -> None:
        self.loads = 0
        self.envelopes: list = []

    def load_account(self, address):
        self.loads += 1
        return Account(address, 4242)

    def simulate_transaction(self, tx):
        self.envelopes.append(tx)
        return _Simulation()


@pytest.fixture()
def server(monkeypatch) -> _Server:
    fake = _Server()
    monkeypatch.setattr(sc, "_server", lambda **_kw: fake)
    monkeypatch.setattr(settings, "stellar_admin_address", _SOURCE)
    return fake


def test_a_sequence_free_read_makes_one_round_trip(server):
    sc.simulate_read(_LEDGER, "rep_state", [sc.sym("agt_01h8")], load_source=False)

    assert server.loads == 0
    assert len(server.envelopes) == 1
    envelope = server.envelopes[0].transaction
    assert envelope.source.account_id == _SOURCE
    # TransactionBuilder bumps the source's sequence once, so 0 goes out as 1.
    assert envelope.sequence == 1


def test_the_default_still_loads_the_source(server):
    """Signed paths and every other reader keep the real sequence."""
    sc.simulate_read(_LEDGER, "rep_state", [sc.sym("agt_01h8")])

    assert server.loads == 1
    assert server.envelopes[0].transaction.sequence == 4243
