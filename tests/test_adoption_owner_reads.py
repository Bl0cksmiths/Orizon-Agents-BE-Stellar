"""`adoption_svc._owners_of`: cached owners, the batch read, and the probes.

Pins the paths the report-level suites do not reach directly: a batch read
that fails falls back to probes, a record with no usable owner is probed, and
a probe that finds the deadline passed leaves its id for the next build.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import settings
from app.services import adoption_svc
from app.stellar import client as sc

REGISTRY = "CREGISTRY"
OWNER_A = "GAOWNERA"
OWNER_B = "GAOWNERB"


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "stellar_agent_registry", REGISTRY)
    adoption_svc.forget_owners()
    yield
    adoption_svc.forget_owners()


def _probes(monkeypatch: pytest.MonkeyPatch, owners: dict[str, str | None]) -> list[str]:
    probed: list[str] = []

    async def owner_or_none(agent_id: str, timeout: float = 0.0) -> str | None:
        probed.append(agent_id)
        return owners.get(agent_id)

    monkeypatch.setattr(adoption_svc, "_owner_or_none", owner_or_none)
    return probed


def _owners_of(ids: list[str], deadline: float | None = None) -> tuple[dict[str, str], bool]:
    return asyncio.run(adoption_svc._owners_of(ids, time.monotonic() + 30 if deadline is None else deadline))


def test_a_failed_batch_read_falls_back_to_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(registry_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        raise ConnectionError("rpc down")

    monkeypatch.setattr(sc, "read_agent_records", broken)
    probed = _probes(monkeypatch, {"a": OWNER_A, "b": None})

    owners, left = _owners_of(["a", "b"])

    assert (owners, left) == ({"a": OWNER_A}, False)
    assert sorted(probed) == ["a", "b"]
    assert adoption_svc._owners_read == {(REGISTRY, "a"): OWNER_A}


def test_a_record_without_a_usable_owner_is_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    records = {"a": {"owner": OWNER_A}, "b": {"owner": ""}, "c": {"owner": 7}, "d": {}}
    monkeypatch.setattr(sc, "read_agent_records", lambda registry_id, ids: records)
    probed = _probes(monkeypatch, {"b": OWNER_B})

    owners, left = _owners_of(["a", "b", "c", "d"])

    assert (owners, left) == ({"a": OWNER_A, "b": OWNER_B}, False)
    assert sorted(probed) == ["b", "c", "d"]


def test_an_owner_read_before_is_reused_without_a_read(monkeypatch: pytest.MonkeyPatch) -> None:
    adoption_svc._owners_read[(REGISTRY, "a")] = OWNER_A

    def unexpected(registry_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        raise AssertionError("no batch read for a known owner")

    monkeypatch.setattr(sc, "read_agent_records", unexpected)
    probed = _probes(monkeypatch, {})

    assert _owners_of(["a"]) == ({"a": OWNER_A}, False)
    assert probed == []


def test_past_the_deadline_nothing_is_read_and_every_id_is_left(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(registry_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        raise AssertionError("no batch read past the deadline")

    monkeypatch.setattr(sc, "read_agent_records", unexpected)
    probed = _probes(monkeypatch, {"a": OWNER_A})

    assert _owners_of(["a", "b"], deadline=time.monotonic() - 1) == ({}, True)
    assert probed == []
    assert adoption_svc._owners_read == {}
