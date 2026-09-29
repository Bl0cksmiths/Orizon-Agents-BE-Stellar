"""The SOW metrics generator pinned against the backend it measures (story 5.05).

The generator imports nothing from `app/` at runtime: it reads the chain and
the deployment. Every server-side fact it re-states — the derived id a
dispute rating is written under, the routes a milestone needs, which
readiness field means "refunds are on", the fields of the network and
reputation documents — is pinned here, so a backend change that would make a
metric read the wrong thing fails in CI instead of miscounting.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.main import app
from app.routers.stellar import NetworkInfo, ReputationParams
from app.services import dispute_rating
from app.services.adoption_svc import AdoptionAgent, AdoptionOperator, AdoptionReport
from scripts.sow_metrics import metrics
from scripts.sow_metrics.config import DEFAULT_TEAM_REGISTER, DISPUTE_ROUTES, MAX_TRACED_STEPS, REGISTER_ROUTE
from scripts.sow_metrics.fakes import met_world
from scripts.sow_metrics.register import load as load_register
from tests.test_sow_metrics_run import run

PACKAGE = Path(__file__).resolve().parents[1] / "scripts" / "sow_metrics"


def _routes() -> set[str]:
    return {f"{method.upper()} {path}" for path, ops in app.openapi()["paths"].items() for method in ops}


@pytest.mark.parametrize(
    "route",
    [
        REGISTER_ROUTE,
        *DISPUTE_ROUTES,
        "GET /readiness",
        "GET /api/stellar/network",
        "GET /api/stellar/reputation/params",
        "GET /api/ecosystem/adoption",
    ],
)
def test_every_route_the_generator_reads_or_requires_exists(route: str) -> None:
    assert route in _routes()


def test_the_route_list_is_served_where_the_generator_reads_it() -> None:
    assert app.openapi_url == "/openapi.json"


@pytest.mark.parametrize("step", [0, 1, 7, 255])
def test_the_dispute_id_is_the_backends(step: int) -> None:
    for seed in ("a", "job-1", "orizon"):
        job = hashlib.sha256(seed.encode()).digest()[:16]
        assert metrics.dispute_job_id(job, step) == dispute_rating.dispute_job_id(job, step)
    assert metrics.DISPUTE_ID_TAG == dispute_rating.DISPUTE_ID_TAG


def test_the_trace_covers_every_step_a_plan_can_have() -> None:
    """The backend packs the step index in two bytes; the trace searches a bounded prefix of that."""
    assert 0 < MAX_TRACED_STEPS <= 2 ** (8 * dispute_rating._STEP_INDEX_BYTES)


def test_the_network_document_carries_the_keys_and_contracts_read() -> None:
    fields = set(NetworkInfo.model_fields)
    assert {"network", "network_passphrase", "admin", "dispatch_signer", "asset", "asset_sac", "contracts"} <= fields


def test_the_reputation_settings_carry_the_floor_fields() -> None:
    assert ReputationParams.model_fields["enabled"].annotation is bool
    assert ReputationParams.model_fields["floor_bps"].annotation is int


def test_the_adoption_report_carries_the_bound_flag_read() -> None:
    """The generator reads `operators[].agents[].agent_id` and `.bound` (True, False or None) and nothing else."""
    assert AdoptionReport.model_fields["operators"].annotation == list[AdoptionOperator]
    assert AdoptionOperator.model_fields["agents"].annotation == list[AdoptionAgent]
    assert AdoptionAgent.model_fields["agent_id"].annotation is str
    assert AdoptionAgent.model_fields["bound"].annotation == bool | None


def test_the_real_readiness_names_the_ratings_keys(client: Any) -> None:
    body = client.get("/readiness").json()
    assert isinstance(body["ratings"], dict)
    assert {"signer", "scorer"} <= set(body["ratings"])


@pytest.mark.parametrize(
    ("refunds", "sweep", "met"),
    [(True, True, True), (False, True, False), (True, False, False), (False, False, False)],
)
def test_refunds_on_is_read_from_the_field_the_backend_computes(
    client: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, refunds: bool, sweep: bool, met: bool
) -> None:
    """The dispute milestone, fed the backend's REAL readiness report: met only when both switches are on."""
    monkeypatch.setattr(settings, "dispute_refunds_enabled", refunds)
    monkeypatch.setattr(settings, "refund_reconcile_enabled", sweep)
    world = met_world()
    real = client.get("/readiness").json()
    world.readiness = {**real, "ratings": world.readiness["ratings"]}
    out = run(world, tmp_path)
    assert (out.status("m08") == "met") is met


def test_the_committed_team_register_loads() -> None:
    team = load_register(Path(__file__).resolve().parents[1] / DEFAULT_TEAM_REGISTER)
    assert len(team.roles) >= 2


def test_nothing_imports_app_at_runtime() -> None:
    for source in PACKAGE.glob("*.py"):
        text = source.read_text()
        assert not re.search(r"^\s*(from app\b|import app\b)", text, re.MULTILINE), source
        for sibling in ("scripts.demo_preflight", "scripts.adoption_report", "scripts.lifecycle"):
            assert sibling not in text, (source, sibling)
