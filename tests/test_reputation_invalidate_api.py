"""The operator route that drops an agent's cached score (D-066).

The uphold script rates the agent from its own process, and until this route
existed it could only invalidate its OWN cache: the running server kept
serving the pre-dispute score for up to a full read TTL (QA measured 91.9 s at
120 s), so plans were routed and stamped on the very number the dispute was
meant to change. These tests drive that exact sequence over HTTP — a cached
read, a rating landing out of process, the stale read it leaves, and the
fresh read the route buys — and hold the route to the fail-closed operator
guard and its own rate limit.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.routers import stellar as stellar_router
from app.services.reputation_svc import STROOPS_PER_USDC
from app.stellar import cache as rcache
from app.stellar import client as sc

AGENT = "code_agent"
LEDGER = "C" + "LEDGER7Q" * 6 + "ABCDEFG"
KEY = "operator-key-" + "4b7e" * 6
ROUTE = f"/api/stellar/reputation/{AGENT}/invalidate"

# Six ratings, two of them disputes — then the upheld dispute's rating lands.
BEFORE = {"sum_w": 6000 * 6 * STROOPS_PER_USDC, "weight": 6 * STROOPS_PER_USDC, "count": 6, "disputed": 2}
AFTER = {"sum_w": 6000 * 6 * STROOPS_PER_USDC, "weight": 7 * STROOPS_PER_USDC, "count": 7, "disputed": 3}


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A ReputationLedger this test can write to behind the server's back.

    The TTL is QA's 120 s, so nothing in a test's lifetime expires on its own:
    any fresh read has to have been bought by an invalidation.
    """
    chain: dict[str, Any] = {"state": dict(BEFORE), "reads": 0}

    def _simulate_read(_contract: str, method: str, _args: list[Any]) -> dict[str, Any]:
        assert method == "rep_state", method
        chain["reads"] += 1
        return dict(chain["state"])

    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", LEDGER)
    monkeypatch.setattr(settings, "reputation_read_ttl_seconds", 120.0)
    monkeypatch.setattr(sc, "simulate_read", _simulate_read)
    monkeypatch.setattr(sc, "contract_ids", lambda: SimpleNamespace(reputation_ledger=LEDGER))
    rcache.clear()
    stellar_router.invalidation_budget.reset()
    yield chain
    rcache.clear()
    stellar_router.invalidation_budget.reset()


def _count(client: Any) -> int:
    response = client.get(f"/api/stellar/reputation/{AGENT}")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "onchain", body
    return int(body["count"])


def _rating_lands_out_of_process(chain: dict[str, Any]) -> None:
    """What `scripts/uphold_dispute.py` does to the ledger: the rating lands,
    and nothing in THIS process hears about it."""
    chain["state"] = dict(AFTER)


def test_without_the_call_the_server_keeps_the_pre_dispute_score(client, ledger) -> None:
    """The defect itself, pinned so the fix below is measured against it."""
    assert _count(client) == 6
    _rating_lands_out_of_process(ledger)

    assert _count(client) == 6
    assert ledger["reads"] == 1


def test_the_operator_key_makes_the_next_read_reflect_the_new_rating(client, ledger, monkeypatch) -> None:
    monkeypatch.setattr(settings, "api_key", KEY)
    assert _count(client) == 6
    _rating_lands_out_of_process(ledger)
    assert _count(client) == 6  # stale, until the operator says otherwise

    response = client.post(ROUTE, headers={"X-API-Key": KEY})

    assert response.status_code == 200
    assert response.json() == {"agent_id": AGENT, "invalidated": True, "read_ttl_seconds": 120.0}
    assert _count(client) == 7
    assert ledger["reads"] == 2


@pytest.mark.parametrize(
    ("configured", "sent", "status", "detail"),
    [
        # API_KEY empty: closed to everyone, including a caller who sends one.
        ("", None, 503, "operator_key_not_configured"),
        ("", KEY, 503, "operator_key_not_configured"),
        # A key configured: no key and a wrong key are one answer.
        (KEY, None, 401, "invalid_api_key"),
        (KEY, KEY + "x", 401, "invalid_api_key"),
    ],
    ids=["no-key-configured", "no-key-configured-key-sent", "no-key-sent", "wrong-key"],
)
def test_the_route_fails_closed_and_leaves_the_cache_alone(
    client, ledger, monkeypatch, configured: str, sent: str | None, status: int, detail: str
) -> None:
    monkeypatch.setattr(settings, "api_key", configured)
    assert _count(client) == 6
    _rating_lands_out_of_process(ledger)

    response = client.post(ROUTE, headers={} if sent is None else {"X-API-Key": sent})

    assert response.status_code == status
    assert response.json()["detail"] == detail
    # Refused means refused: the cached entry is still what the next read gets.
    assert _count(client) == 6
    assert ledger["reads"] == 1


def test_admitted_calls_are_rate_limited_and_refused_ones_do_not_spend_the_budget(client, ledger, monkeypatch) -> None:
    monkeypatch.setattr(settings, "api_key", KEY)
    monkeypatch.setattr(stellar_router.invalidation_budget, "limit", 2)

    for _ in range(5):
        assert client.post(ROUTE, headers={"X-API-Key": "guess"}).status_code == 401
    assert client.post(ROUTE, headers={"X-API-Key": KEY}).status_code == 200
    assert client.post(ROUTE, headers={"X-API-Key": KEY}).status_code == 200

    limited = client.post(ROUTE, headers={"X-API-Key": KEY})
    assert limited.status_code == 429
    assert limited.json()["detail"] == "rate_limited"


def test_an_invalid_agent_id_is_refused_before_anything_is_touched(client, ledger, monkeypatch) -> None:
    monkeypatch.setattr(settings, "api_key", KEY)
    response = client.post("/api/stellar/reputation/not-a-symbol/invalidate", headers={"X-API-Key": KEY})
    assert response.status_code == 422
