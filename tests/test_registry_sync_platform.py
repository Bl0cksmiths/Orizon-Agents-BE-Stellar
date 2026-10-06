"""The mirror checks the built-in agents' on-chain records, and never mirrors them (ADR 0016).

Once the built-in catalog is registered to the platform treasury, `list_ids`
names every `agt_` id. Pinned here:

  * a built-in record is read (in the pass's one batch, never with `get`) and
    compared with the terms `platform_treasury` derives from the catalog — owner,
    name, skills, price and `active` — and the verdict is kept per id;
  * it is NEVER upserted: the catalog agent stays `source="seeded"`, `owner`
    None, `real` True, at its seeded price, so the plan price, the executor
    stamp and every count over `state.agents` are what they were;
  * one log line per id per verdict, not one per pass;
  * an `agt_` id the platform has no worker for is skipped, as before.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
import test_registry_sync_batch as batch
from test_registry_sync_batch import _onchain, _raw

from app.agents.registry import WORKERS
from app.schemas import PlanStep
from app.seed import seed_registry
from app.services import platform_treasury, registry_sync
from app.services.orchestrator_svc import _with_executor, plannable
from app.state import state

LOGGER_NAME = "app.services.registry_sync"
TREASURY = platform_treasury.treasury_address()

# The batch suite's fake registry: `list_ids`, the batched record read and `get`.
registry = batch.registry


@pytest.fixture(autouse=True)
def _catalog() -> Any:
    seed_registry()
    registry_sync._platform.clear()
    registry_sync._platform_logged.clear()
    yield
    registry_sync._platform.clear()
    registry_sync._platform_logged.clear()


def _registered(agent_id: str, **overrides: Any) -> dict[str, Any]:
    """The record `register(treasury, …)` with the catalog's terms leaves on-chain."""
    terms = platform_treasury.registration_for(agent_id)
    record = _raw(agent_id, owner=TREASURY, name=terms.name, skills=list(terms.skills), price=terms.price_stroops)
    record.update(overrides)
    return record


def _all_registered() -> dict[str, dict[str, Any]]:
    return {agent_id: _registered(agent_id) for agent_id in sorted(WORKERS)}


def test_registered_built_in_agents_stay_the_seeded_catalog(registry: Any) -> None:
    seeded = {agent_id: state.agents[agent_id] for agent_id in WORKERS}
    fake = registry({**_all_registered(), "ext_a": _raw("ext_a")})

    assert asyncio.run(registry_sync.sync_once()) == 1  # only the operator agent is mirrored

    assert {agent_id: state.agents[agent_id] for agent_id in WORKERS} == seeded
    assert fake.gets == []
    assert sorted(_onchain()) == ["ext_a"]
    agents = state.list_agents()
    assert sum(1 for a in agents if a.source == "seeded") == len(WORKERS)
    assert sum(1 for a in agents if a.source == "onchain") == 1
    assert registry_sync.status().synced is True


def test_every_registered_built_in_agent_is_verified(registry: Any) -> None:
    registry(_all_registered())

    asyncio.run(registry_sync.sync_once())

    assert registry_sync.platform_agents() == dict.fromkeys(sorted(WORKERS), "registered")


def test_a_registered_built_in_agent_is_still_planned_as_built_in(registry: Any) -> None:
    registry(_all_registered())
    asyncio.run(registry_sync.sync_once())

    agent = state.agents["agt_11c0"]
    step = _with_executor(PlanStep(agent_id="agt_11c0", rationale="r", price_stroops=540_000, est_eta_seconds=1.0))

    assert plannable(agent) is True
    assert step.executor == "built_in"
    assert step.price_stroops == platform_treasury.registration_for("agt_11c0").price_stroops


def test_an_on_chain_reprice_changes_nothing_but_is_reported(registry: Any, caplog: Any) -> None:
    records = _all_registered()
    records["agt_01h8"] = _registered("agt_01h8", price=999_999)
    registry(records)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        asyncio.run(registry_sync.sync_once())
        asyncio.run(registry_sync.sync_once())

    assert state.agents["agt_01h8"].price == 0.012
    assert registry_sync.platform_agents()["agt_01h8"] == "mismatch"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "agt_01h8" in r.getMessage()]
    assert len(warnings) == 1
    assert "price 999999 stroops, the catalog's is 120000" in warnings[0]


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("name", "copywrite.v4", "name"),
        ("skills", ["copy"], "skills"),
        ("active", False, "inactive"),
    ],
)
def test_any_other_term_that_differs_is_a_mismatch(registry: Any, field: str, value: Any, issue: str) -> None:
    registry({"agt_01h8": _registered("agt_01h8", **{field: value})})

    asyncio.run(registry_sync.sync_once())

    assert registry_sync.platform_agents()["agt_01h8"] == "mismatch"
    assert any(issue in i for i in registry_sync.platform_issues()["agt_01h8"])


def test_a_built_in_id_somebody_else_registered_is_reported_and_not_mirrored(registry: Any, caplog: Any) -> None:
    seeded = state.agents["agt_01h8"]
    registry({"agt_01h8": _raw("agt_01h8", name="CLOBBERED")})  # owned by the admin key, not the treasury

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        asyncio.run(registry_sync.sync_once())

    assert state.agents["agt_01h8"] == seeded
    assert registry_sync.platform_agents()["agt_01h8"] == "foreign_owner"
    [warning] = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "agt_01h8" in r.getMessage()
    ]
    assert "not the platform treasury" in warning


def test_an_unregistered_built_in_agent_says_so(registry: Any) -> None:
    records = _all_registered()
    del records["agt_12r0"]
    registry(records)

    asyncio.run(registry_sync.sync_once())

    verdicts = registry_sync.platform_agents()
    assert verdicts["agt_12r0"] == "unregistered"
    assert sum(1 for v in verdicts.values() if v == "registered") == len(WORKERS) - 1


def test_a_record_the_batch_could_not_read_is_unread_and_never_fetched_with_get(registry: Any) -> None:
    fake = registry(_all_registered(), batch_skips={"agt_01h8"})

    asyncio.run(registry_sync.sync_once())

    assert registry_sync.platform_agents()["agt_01h8"] == "unread"
    assert fake.gets == []
    assert registry_sync.status().synced is True  # a built-in record never holds the mirror partial


def test_before_any_pass_every_built_in_agent_is_unread() -> None:
    assert registry_sync.platform_agents() == dict.fromkeys(sorted(WORKERS), "unread")


def test_an_agt_id_with_no_worker_is_skipped_unread(registry: Any) -> None:
    fake = registry({"agt_squat": _raw("agt_squat"), "ext_a": _raw("ext_a")})

    asyncio.run(registry_sync.sync_once())

    assert fake.batches == [["ext_a"]]
    assert "agt_squat" not in state.agents
    assert "agt_squat" not in registry_sync.platform_agents()
