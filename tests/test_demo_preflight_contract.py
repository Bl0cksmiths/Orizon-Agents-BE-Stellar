"""The demo pre-flight pinned against the backend it checks (story 5.04).

The pre-flight imports nothing from `app/` at runtime: it reads the deployment
over HTTP. Every server-side fact it re-states — a route, a field name, which
readiness field means "refunds are on", which steps make an agent ready — is
pinned here, so a backend change that would make a check read the wrong field
fails in CI instead of passing a NO-GO deployment as GO.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings, settings
from app.main import app
from app.routers.stellar import ReputationInfo, ReputationParams
from app.schemas import Agent, PlanFloorNotice
from app.services import operator_readiness
from scripts.demo_preflight import checks
from scripts.demo_preflight.api import Answer
from scripts.demo_preflight.config import DEFAULT_MAX_REFUND, DEFAULT_TEAM_REGISTER
from scripts.demo_preflight.register import load as load_register

PACKAGE = Path(__file__).resolve().parents[1] / "scripts" / "demo_preflight"


def _paths() -> dict[str, Any]:
    return app.openapi()["paths"]


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/health", "get"),
        ("/api/stellar/network", "get"),
        ("/api/ecosystem/adoption", "get"),
        ("/api/agents", "get"),
        ("/api/agents/{agent_id}/readiness", "get"),
        ("/api/stellar/reputation", "get"),
        ("/api/stellar/reputation/params", "get"),
        ("/readiness", "get"),
        ("/api/orchestrator/decompose", "post"),
    ],
)
def test_every_route_the_preflight_calls_exists(path: str, method: str) -> None:
    assert method in _paths()[path]


def _readiness_body(client: Any) -> dict[str, Any]:
    body = client.get("/readiness").json()
    assert isinstance(body, dict)
    return body


def _backend_check(body: dict[str, Any], escrow: str | None) -> checks.Check:
    reads = SimpleNamespace(
        api="https://api.test",
        backend="https://be.test",
        readiness=lambda: Answer("https://be.test/readiness", 200, {**body, "status": "ready"}),
    )
    facts = checks.Facts(network={"contracts": {"payment_escrow": escrow}} if escrow else {})
    return checks.read_backend_readiness(reads, facts)  # type: ignore[arg-type]


def test_the_real_readiness_carries_every_5_01_field_the_preflight_reads(client: Any) -> None:
    body = _readiness_body(client)
    check = _backend_check(body, None)
    assert check.status == checks.PASS, check.detail
    # The settler is compared against this field.
    assert "signer" in body["ratings"]


def test_a_readiness_without_the_5_01_fields_is_caught(client: Any) -> None:
    body = _readiness_body(client)
    body.pop("escrow")
    assert "predates story 5.01" in _backend_check(body, None).detail


@pytest.mark.parametrize(
    ("refunds", "sweep", "expected"),
    [(True, True, checks.PASS), (False, True, checks.FAIL), (True, False, checks.FAIL), (False, False, checks.FAIL)],
)
def test_refunds_on_is_read_from_the_field_the_backend_computes(
    client: Any, monkeypatch: pytest.MonkeyPatch, refunds: bool, sweep: bool, expected: str
) -> None:
    """`disputes.reconcile.enabled` is REFUND_RECONCILE_ENABLED AND DISPUTE_REFUNDS_ENABLED; it is the only
    public field true only when the refund switch is on, and it is what `refunds.enabled` reads."""
    monkeypatch.setattr(settings, "dispute_refunds_enabled", refunds)
    monkeypatch.setattr(settings, "refund_reconcile_enabled", sweep)
    facts = checks.Facts(readiness=_readiness_body(client))
    assert checks.check_refunds_enabled(facts).status == expected


def test_ready_steps_are_the_backends_ready_keys_in_its_order() -> None:
    assert set(checks.READY_STEPS) == set(operator_readiness.READY_KEYS)
    order = [k for k in operator_readiness.STEP_KEYS if k in operator_readiness.READY_KEYS]
    assert list(checks.READY_STEPS) == order


def test_first_unready_step_reads_a_real_readiness() -> None:
    steps = tuple(
        operator_readiness.Step(key=k, status="failed" if k == "reachable" else "done", detail=k)
        for k in operator_readiness.STEP_KEYS
    )
    body = {"ready": operator_readiness.is_ready(steps), "steps": [s.__dict__ for s in steps]}
    step = checks.first_unready_step(body)
    assert body["ready"] is False and step is not None and step["key"] == "reachable"


def test_the_exclusion_notice_kinds_exist() -> None:
    kinds = set(typing.get_args(PlanFloorNotice.model_fields["kind"].annotation))
    assert {"excluded", "substituted"} <= kinds


def test_the_reputation_fields_the_floor_check_reads_exist() -> None:
    assert {"lower_bound_bps", "degraded", "stale"} <= set(ReputationInfo.model_fields)
    assert {"enabled", "floor_bps"} <= set(ReputationParams.model_fields)
    assert {"id", "source", "owner", "bound"} <= set(Agent.model_fields)
    assert "onchain" in typing.get_args(Agent.model_fields["source"].annotation)


def test_the_assumed_refund_cap_is_the_backends_default() -> None:
    assert Settings.model_fields["max_refund_usdc"].default == DEFAULT_MAX_REFUND


def test_the_committed_team_register_loads() -> None:
    team = load_register(Path(__file__).resolve().parents[1] / DEFAULT_TEAM_REGISTER)
    assert len(team.roles) >= 2


def test_nothing_imports_app_at_runtime() -> None:
    for source in PACKAGE.glob("*.py"):
        text = source.read_text()
        assert not re.search(r"^\s*(from app\b|import app\b)", text, re.MULTILINE), source
        assert "from ..app" not in text and "scripts.lifecycle" not in text, source
