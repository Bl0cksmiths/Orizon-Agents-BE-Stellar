"""The authorization the buyer signs is the plan's total, to the stroop (ADR 0015).

`POST /api/stellar/build/authorize` builds the escrow's `authorize(payer,
label, max_amount, expires_at)`. The label is the plan id (finding S2), so
when this service holds that plan the amount it is asked for can be checked
against what the plan card showed — and an amount that is not the plan's total
is refused before the wallet is asked to sign custody of it. The amount may be
sent as exact `max_amount_stroops`; the legacy float still works and converts
by the one rule.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from stellar_sdk import Keypair, scval

from app.schemas import Plan, PlanStep, StoredPlan
from app.services import task_persistence
from app.state import state
from app.stellar import client as sc

PAYER = Keypair.from_raw_ed25519_seed(bytes(range(32))).public_key
PLAN_ID = "pln_auth0001"
ROUTE = "/api/stellar/build/authorize"


@pytest.fixture()
def built(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    """The `authorize` arguments the route asked the client to build."""
    calls: list[list[Any]] = []

    def _build(contract_id: str, fn: str, args: list[Any], *, source: str) -> str:
        assert fn == "authorize"
        calls.append(args)
        return "AAAA"

    monkeypatch.setattr(sc, "build_invoke_xdr", _build)
    return calls


@pytest.fixture()
def held_plan() -> Iterator[StoredPlan]:
    plan = StoredPlan(
        id=PLAN_ID,
        intent="x",
        plan=Plan(
            steps=[
                PlanStep(agent_id="agt_01h8", rationale="r", price_stroops=120_000, est_eta_seconds=1.0),
                PlanStep(agent_id="agt_05x7", rationale="r", price_stroops=90_000, est_eta_seconds=1.0),
            ]
        ),
        total_eta=2.0,
    )
    state.add_plan(plan)
    yield plan
    state.plans.pop(PLAN_ID, None)


def _amount(args: list[Any]) -> int:
    return scval.to_native(args[2])


def test_the_exact_total_in_stroops_is_what_gets_built(
    client: TestClient, built: list[list[Any]], held_plan: StoredPlan
) -> None:
    r = client.post(ROUTE, json={"payer": PAYER, "agent_id": PLAN_ID, "max_amount_stroops": 210_000})

    assert r.status_code == 200, r.text
    assert _amount(built[0]) == held_plan.plan.total_stroops == 210_000


def test_the_legacy_float_total_still_builds_the_same_amount(
    client: TestClient, built: list[list[Any]], held_plan: StoredPlan
) -> None:
    """The console signs `total_usdc` today: 0.021, never 0.020999999999999998."""
    r = client.post(ROUTE, json={"payer": PAYER, "agent_id": PLAN_ID, "max_amount_usdc": held_plan.total_usdc})

    assert r.status_code == 200, r.text
    assert _amount(built[0]) == 210_000


@pytest.mark.parametrize(
    "amount",
    [{"max_amount_stroops": 209_999}, {"max_amount_stroops": 210_001}, {"max_amount_usdc": 0.001}],
    ids=["a-stroop-short", "a-stroop-over", "the-consoles-0.001-fallback"],
)
def test_an_amount_that_is_not_the_held_plans_total_is_refused(
    client: TestClient, built: list[list[Any]], held_plan: StoredPlan, amount: dict[str, Any]
) -> None:
    r = client.post(ROUTE, json={"payer": PAYER, "agent_id": PLAN_ID, **amount})

    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "authorization_amount_mismatch"
    assert "210000 stroops" in error["message"]
    assert built == []  # nothing for the wallet to sign


def test_a_label_naming_no_held_plan_is_built_as_asked(client: TestClient, built: list[list[Any]]) -> None:
    """A plan this process does not hold cannot be checked here; `/execute` still is."""
    r = client.post(ROUTE, json={"payer": PAYER, "agent_id": "pln_elsewhere", "max_amount_stroops": 12_345})

    assert r.status_code == 200, r.text
    assert _amount(built[0]) == 12_345


def test_an_unreadable_plan_store_does_not_block_the_build(
    client: TestClient, built: list[list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _down(_plan_id: str) -> StoredPlan | None:
        raise task_persistence.TaskStoreUnavailable("down")

    monkeypatch.setattr(task_persistence, "load_plan", _down)

    r = client.post(ROUTE, json={"payer": PAYER, "agent_id": PLAN_ID, "max_amount_stroops": 210_000})

    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "amount",
    [
        {},
        {"max_amount_stroops": 0},
        {"max_amount_stroops": -5},
        {"max_amount_stroops": 10_000 * 10_000_000 + 1},
        {"max_amount_stroops": 210_000, "max_amount_usdc": 0.5},
    ],
    ids=["none", "zero", "negative", "over-the-route-bound", "two-amounts-that-disagree"],
)
def test_a_malformed_amount_is_refused_before_anything_is_built(
    client: TestClient, built: list[list[Any]], amount: dict[str, Any]
) -> None:
    r = client.post(ROUTE, json={"payer": PAYER, "agent_id": "pln_whatever", **amount})

    assert r.status_code == 422, r.text
    assert built == []


def test_two_amounts_that_agree_are_accepted(client: TestClient, built: list[list[Any]], held_plan: StoredPlan) -> None:
    r = client.post(
        ROUTE, json={"payer": PAYER, "agent_id": PLAN_ID, "max_amount_stroops": 210_000, "max_amount_usdc": 0.021}
    )

    assert r.status_code == 200, r.text
    assert _amount(built[0]) == 210_000
