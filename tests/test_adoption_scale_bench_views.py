"""Characterization tests for the scale bench's fake RPC contract views.

`FakeRpc._view` answers the registry, escrow and token views the adoption
build simulates. Each answer is pinned as XDR, an unknown agent surfaces as a
`KeyError` (which `simulate_transaction` turns into a contract error), and a
view the bench does not model is an assertion, never a silent default.
"""

from __future__ import annotations

import pytest
from stellar_sdk import scval

from scripts.adoption_scale_bench import ADMIN, ESCROW, REGISTRY, SAC, SETTLER, FakeRpc


@pytest.fixture(scope="module")
def rpc() -> FakeRpc:
    return FakeRpc(3, scale=0.0, timeout_rate=0.0, seed=0)


def test_registry_views(rpc: FakeRpc) -> None:
    ids = rpc.ids
    assert len(ids) == 23 and ids[0] == "ext_00000" and ids[-1] == "team_19"
    assert rpc._view(REGISTRY, "list_ids", []) == scval.to_vec([scval.to_symbol(i) for i in ids])
    assert rpc._view(REGISTRY, "get", ["ext_00001"]) == rpc._record("ext_00001")
    owner = rpc.records["ext_00002"]["owner"]
    assert rpc._view(REGISTRY, "owner_of", ["ext_00002"]) == scval.to_address(owner)
    assert rpc._view(REGISTRY, "admin", []) == scval.to_address(ADMIN)


def test_escrow_and_token_views(rpc: FakeRpc) -> None:
    assert rpc._view(ESCROW, "settler", []) == scval.to_address(SETTLER)
    assert rpc._view(ESCROW, "admin", []) == scval.to_address(ADMIN)
    auth_hex, payer = next(iter(rpc.payers.items()))
    expected = scval.to_map({scval.to_symbol("payer"): scval.to_address(payer)})
    assert rpc._view(ESCROW, "authorization", [bytes.fromhex(auth_hex)]) == expected
    assert rpc._view(SAC, "name", []) == scval.to_string("native")


@pytest.mark.parametrize(
    ("contract", "fn", "args"),
    [(REGISTRY, "get", ["nobody"]), (REGISTRY, "owner_of", ["nobody"]), (ESCROW, "authorization", [b"\x00" * 16])],
)
def test_an_unknown_key_is_a_key_error(rpc: FakeRpc, contract: str, fn: str, args: list[object]) -> None:
    with pytest.raises(KeyError):
        rpc._view(contract, fn, args)


@pytest.mark.parametrize(
    ("contract", "fn"), [(REGISTRY, "settler"), (ESCROW, "list_ids"), (SAC, "admin"), ("CUNKNOWN", "name")]
)
def test_a_view_the_bench_does_not_model_is_an_assertion(rpc: FakeRpc, contract: str, fn: str) -> None:
    with pytest.raises(AssertionError, match=rf"^unexpected view {contract}\.{fn}$"):
        rpc._view(contract, fn, [])
