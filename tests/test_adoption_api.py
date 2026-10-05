"""GET /api/ecosystem/adoption — the frozen shape, and what it must never say.

The frontend codes against this payload as it stands, so the snapshot below is
the contract: a renamed, added or dropped key fails here before it fails in a
reviewer's browser. And because the route is public, it pins that an
operator's endpoint URL never leaves the service through it in any form.

Driven without lifespan (a bare TestClient), so no registry-sync loop runs:
the world under test is exactly the one the `world` fixture builds.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_adoption_svc import (
    AT_UNIX,
    BUYER,
    DISPATCH,
    EXT_A,
    EXT_B,
    QA_BUYER,
    QA_OPERATOR,
    REGISTER,
    _entry,
    _hash,
    _job,
    _Store,
    _World,
)
from test_adoption_svc import world as world  # noqa: F401 — the shared fixture

from app.main import app
from app.services import adoption_svc
from app.services.binding_store import InMemoryBindingStore
from app.stellar import cache as rcache

GENERATED_AT = 1_759_046_400
TX = "https://stellar.expert/explorer/testnet/tx/"
ACCOUNT = "https://stellar.expert/explorer/testnet/account/"
ADMIN = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"


async def _build() -> None:
    """Run one report build to completion, as the background refresher would."""
    task = adoption_svc.report_cell.refresh(force=True)
    assert task is not None
    await task


def _get(client: TestClient) -> Any:
    """The route as it answers once a report has been built."""
    rcache.clear()
    adoption_svc.report_cell.reset()
    asyncio.run(_build())
    return client.get("/api/ecosystem/adoption")


def test_the_response_is_exactly_the_frozen_shape(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adoption_svc.time, "time", lambda: GENERATED_AT)
    world.store = _Store("ext_a1")
    world.agent("ext_a1", EXT_A, _entry(1, amount=100_000), _entry(2, payer=QA_BUYER, amount=250_000))
    world.agent("ext_b1", EXT_B, active=False)
    world.agent("uat605_ext_op", QA_OPERATOR, _entry(3))
    world.agent("dispatch_owned", DISPATCH)
    world.agent("unread", EXT_B)
    world.settlements["unread"] = RuntimeError("rpc down")

    response = _get(TestClient(app))

    assert response.status_code == 200
    assert response.json() == {
        "network": "testnet",
        "generated_at": GENERATED_AT,
        "window_days": 7.0,
        "targets": {"external_agents": 2, "unique_operator_wallets": 2, "settled_external_workflows": 3},
        "totals": {"external_agents": 3, "unique_operator_wallets": 2, "settled_external_workflows": 2},
        "met": {"external_agents": True, "unique_operator_wallets": True, "settled_external_workflows": False},
        "operators": sorted(
            [
                {
                    "owner": EXT_A,
                    "owner_explorer": ACCOUNT + EXT_A,
                    "agents": [
                        {
                            "agent_id": "ext_a1",
                            "name": "ext_a1 name",
                            "active": True,
                            "bound": True,
                            "settled_workflows": [
                                {
                                    "job_id_hex": _job(1),
                                    "tx_hash": _hash(1),
                                    "explorer": TX + _hash(1),
                                    "amount_usdc": 0.01,
                                    "payer": BUYER,
                                    "payer_team_role": None,
                                    "settled_at": AT_UNIX,
                                },
                                {
                                    "job_id_hex": _job(2),
                                    "tx_hash": _hash(2),
                                    "explorer": TX + _hash(2),
                                    "amount_usdc": 0.025,
                                    "payer": QA_BUYER,
                                    "payer_team_role": REGISTER[QA_BUYER],
                                    "settled_at": AT_UNIX,
                                },
                            ],
                        }
                    ],
                },
                {
                    "owner": EXT_B,
                    "owner_explorer": ACCOUNT + EXT_B,
                    "agents": [
                        {
                            "agent_id": "ext_b1",
                            "name": "ext_b1 name",
                            "active": False,
                            "bound": False,
                            "settled_workflows": [],
                        },
                        {
                            "agent_id": "unread",
                            "name": "unread name",
                            "active": True,
                            "bound": False,
                            "settled_workflows": [],
                        },
                    ],
                },
            ],
            key=lambda op: op["owner"],
        ),
        "excluded": sorted(
            [
                {
                    "owner": QA_OPERATOR,
                    "owner_explorer": ACCOUNT + QA_OPERATOR,
                    "reason": "team_wallet",
                    "role": "QA throwaway operator key",
                    "agent_ids": ["uat605_ext_op"],
                },
                {
                    "owner": DISPATCH,
                    "owner_explorer": ACCOUNT + DISPATCH,
                    "reason": "platform_key",
                    "role": "dispatch signer",
                    "agent_ids": ["dispatch_owned"],
                },
            ],
            key=lambda row: row["owner"],
        ),
        "degraded": True,
        "unreadable_agents": ["unread"],
    }


def test_no_endpoint_url_ever_appears_in_the_response(world: _World) -> None:
    """A real binding store holding a real record: the agent reads as bound,
    and nothing of the URL — scheme, host, path, query — leaves the route."""
    store = InMemoryBindingStore()
    url = "https://operator-secret-host.example.net/private/hook?token=hunter2"
    asyncio.run(store.put("ext_a1", url, EXT_A))
    world.store = store
    world.agent("ext_a1", EXT_A, _entry(1))

    response = _get(TestClient(app))

    assert response.status_code == 200
    assert response.json()["operators"][0]["agents"][0]["bound"] is True
    for fragment in ("operator-secret-host", "example.net", "/private/hook", "hunter2", "token="):
        assert fragment not in response.text
    assert "endpoint" not in response.text


def test_the_route_sits_under_the_service_rate_limiter(world: _World) -> None:
    response = _get(TestClient(app))

    assert response.status_code == 200
    assert "x-ratelimit-limit" in response.headers
    assert "x-ratelimit-remaining" in response.headers


def test_a_report_that_cannot_be_produced_is_503_never_a_zero(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken() -> adoption_svc.AdoptionReport:
        raise RuntimeError("cache layer failed")

    monkeypatch.setattr(adoption_svc, "build_report", broken)
    asyncio.run(_build())  # fails, and the cell backs off before the next attempt

    response = TestClient(app).get("/api/ecosystem/adoption")

    assert response.status_code == 503
    assert response.json()["detail"] == "adoption_unavailable"
    assert response.headers["retry-after"] == str(int(adoption_svc.REPORT_RETRY_AFTER_FAILURE_SECONDS))
    assert "totals" not in response.text


def test_the_platform_admin_owning_agents_is_listed_not_hidden(world: _World) -> None:
    world.agent("algorex", ADMIN)
    world.agent("keyboardai", ADMIN)

    body = _get(TestClient(app)).json()

    assert body["totals"] == {"external_agents": 0, "unique_operator_wallets": 0, "settled_external_workflows": 0}
    assert body["excluded"] == [
        {
            "owner": ADMIN,
            "owner_explorer": ACCOUNT + ADMIN,
            "reason": "team_wallet",
            "role": REGISTER[ADMIN],
            "agent_ids": ["algorex", "keyboardai"],
        }
    ]
