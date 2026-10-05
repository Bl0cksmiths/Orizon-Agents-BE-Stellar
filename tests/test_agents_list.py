"""GET /api/agents: the unchanged full list, pages, field selection and validators.

The no-parameter answer is the contract every current client parses, so it is
pinned first: the whole mirror, in the mirror's order, with the registry
headers. Pagination is keyset by id, so its edges are walked explicitly — an
exact multiple, past the end, a cursor whose agent has since been delisted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from app.routers import agents as agents_router
from app.schemas import Agent
from app.services import binding_registry, registry_sync
from app.state import state

URL = "/api/agents"


def _agent(agent_id: str, *, source: str = "onchain") -> Agent:
    return Agent(
        id=agent_id,
        name=f"{agent_id} name",
        skills=["code"],
        price=0.02,
        rep=4.0,
        status="online",
        runs=0,
        owner="GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV" if source == "onchain" else None,
        source=source,
    )


@pytest.fixture
def registry(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[int], list[str]]]:
    """Replace the mirror with `n` on-chain agents inserted in REVERSE id order,
    so the mirror's order and id order differ. Returns the ids, mirror order."""
    saved = dict(state.agents)
    monkeypatch.setattr(binding_registry, "_bound_ids", set())
    monkeypatch.setattr(binding_registry, "_loaded", True)
    monkeypatch.setattr(registry_sync, "status", lambda: registry_sync.SyncStatus(synced=True))

    def use(n: int) -> list[str]:
        state.agents.clear()
        ids = [f"ag_{i:03d}" for i in reversed(range(n))]
        for agent_id in ids:
            state.add_agent(_agent(agent_id))
        return ids

    yield use
    state.agents.clear()
    state.agents.update(saved)


def _walk(client: TestClient, limit: int) -> tuple[list[str], int]:
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        r = client.get(URL, params=params)
        assert r.status_code == 200, r.text
        rows = r.json()
        assert r.headers["x-registry-count"] == str(len(rows))
        seen += [row["id"] for row in rows]
        pages += 1
        cursor = r.headers.get("x-next-cursor")
        if cursor is None:
            return seen, pages
        assert pages < 100


# ── the unchanged full list ─────────────────────────────────────────────────
def test_no_parameters_is_the_whole_mirror_in_its_own_order(client: TestClient, registry) -> None:
    ids = registry(25)
    r = client.get(URL)
    assert r.status_code == 200
    body = r.json()
    assert [row["id"] for row in body] == ids
    assert set(body[0]) == set(Agent.model_fields)
    assert body[0]["bound"] is False
    assert r.headers["x-registry-synced"] == "true"
    assert r.headers["x-registry-count"] == r.headers["x-total-count"] == "25"
    assert "x-next-cursor" not in r.headers


def test_the_list_revalidates_and_is_never_served_unasked(client: TestClient, registry) -> None:
    registry(5)
    first = client.get(URL)
    assert first.headers["etag"].startswith('W/"')
    assert first.headers["cache-control"] == "no-cache"
    again = client.get(URL, headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["x-registry-synced"] == "true"


def test_a_binding_changes_the_etag(client: TestClient, registry) -> None:
    ids = registry(3)
    first = client.get(URL)
    binding_registry._bound_ids.add(ids[0])
    second = client.get(URL, headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 200
    assert second.headers["etag"] != first.headers["etag"]
    assert second.json()[0]["bound"] is True


def test_a_large_list_leaves_gzipped(client: TestClient, registry) -> None:
    registry(300)
    r = client.get(URL, headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert len(r.json()) == 300


# ── pages ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("n", "limit", "pages"), [(25, 7, 4), (21, 7, 3), (5, 1000, 1), (0, 10, 1), (1, 1, 1)])
def test_paging_walks_every_agent_once_in_id_order(
    client: TestClient, registry, n: int, limit: int, pages: int
) -> None:
    ids = registry(n)
    seen, walked = _walk(client, limit)
    assert seen == sorted(ids)
    assert walked == pages


def test_a_page_reports_the_whole_total(client: TestClient, registry) -> None:
    registry(25)
    r = client.get(URL, params={"limit": 10})
    assert r.headers["x-total-count"] == "25"
    assert r.headers["x-registry-count"] == "10"
    assert r.headers["x-next-cursor"] == agents_router.encode_cursor("ag_009")


def test_a_cursor_alone_pages_by_the_default_size(client: TestClient, registry) -> None:
    registry(agents_router.DEFAULT_PAGE_SIZE + 30)
    r = client.get(URL, params={"cursor": agents_router.encode_cursor("ag_009")})
    rows = r.json()
    assert len(rows) == agents_router.DEFAULT_PAGE_SIZE
    assert rows[0]["id"] == "ag_010"


def test_a_cursor_past_the_end_is_an_empty_last_page(client: TestClient, registry) -> None:
    registry(5)
    r = client.get(URL, params={"limit": 5, "cursor": agents_router.encode_cursor("zz_last")})
    assert r.status_code == 200
    assert r.json() == []
    assert "x-next-cursor" not in r.headers


def test_a_cursor_whose_agent_was_delisted_still_continues_after_it(client: TestClient, registry) -> None:
    registry(10)
    first = client.get(URL, params={"limit": 4})
    cursor = first.headers["x-next-cursor"]
    del state.agents["ag_003"]  # the page's last agent leaves the registry
    second = client.get(URL, params={"limit": 4, "cursor": cursor}).json()
    assert [row["id"] for row in second] == ["ag_004", "ag_005", "ag_006", "ag_007"]


def test_an_agent_registered_between_pages_is_not_skipped_or_repeated(client: TestClient, registry) -> None:
    registry(6)
    first = client.get(URL, params={"limit": 3})
    state.add_agent(_agent("ag_004a"))
    second = client.get(URL, params={"limit": 10, "cursor": first.headers["x-next-cursor"]}).json()
    assert [row["id"] for row in second] == ["ag_003", "ag_004", "ag_004a", "ag_005"]


@pytest.mark.parametrize("limit", ["0", "-1", "1001", "ten"])
def test_an_out_of_range_limit_is_422(client: TestClient, registry, limit: str) -> None:
    registry(3)
    assert client.get(URL, params={"limit": limit}).status_code == 422


@pytest.mark.parametrize("cursor", ["!!!", "bm90IGFuIGlkIQ", "x" * 65])
def test_a_cursor_this_endpoint_did_not_issue_is_refused(client: TestClient, registry, cursor: str) -> None:
    registry(3)
    r = client.get(URL, params={"cursor": cursor})
    assert r.status_code in (400, 422)
    if r.status_code == 400:
        assert r.json()["error"]["code"] == "invalid_cursor"


# ── fields ──────────────────────────────────────────────────────────────────
def test_fields_keeps_only_those_keys_and_always_the_id(client: TestClient, registry) -> None:
    registry(3)
    rows = client.get(URL, params={"fields": "name, price"}).json()
    assert [set(row) for row in rows] == [{"id", "name", "price"}] * 3


def test_fields_can_ask_for_the_read_time_bound_flag(client: TestClient, registry) -> None:
    ids = registry(2)
    binding_registry._bound_ids.add(ids[1])
    rows = client.get(URL, params={"fields": "bound"}).json()
    assert rows == [{"id": ids[0], "bound": False}, {"id": ids[1], "bound": True}]


def test_fields_combine_with_paging(client: TestClient, registry) -> None:
    registry(5)
    r = client.get(URL, params={"fields": "status", "limit": 2})
    assert r.json() == [{"id": "ag_000", "status": "online"}, {"id": "ag_001", "status": "online"}]
    assert r.headers["x-next-cursor"]


@pytest.mark.parametrize("fields", ["nope", "name,secret", ",", " "])
def test_an_unknown_field_is_422_naming_the_allowed_ones(client: TestClient, registry, fields: str) -> None:
    registry(2)
    r = client.get(URL, params={"fields": fields})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_fields"
    assert "allowed: id, name" in r.json()["error"]["message"]


def test_a_cross_origin_caller_may_read_the_paging_and_snapshot_headers(client: TestClient, registry) -> None:
    registry(3)
    r = client.get(URL, params={"limit": 1}, headers={"Origin": "https://orizon-agents-fe-stellar.vercel.app"})
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {
        "x-total-count",
        "x-next-cursor",
        "x-snapshot-age",
        "x-snapshot-source",
        "etag",
        "retry-after",
    } <= exposed
