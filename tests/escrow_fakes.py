"""Fakes for PaymentEscrow v2 reads, shared by the authorization guard's tests.

Faked at the client seam, `sc.simulate_read`, so every outcome the escrow can
answer is driven exactly and nothing reaches testnet.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.schemas import Plan, PlanStep, StoredPlan
from app.stellar import client as sc

ESCROW = "CESCROWV2TESTESCROWV2TESTESCROWV2TESTESCROWV2TESTESCROW2"
PAYER = "GBUYERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
OTHER = "GOTHERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
AUTH = "ab" * 16
NOW = 1_800_000_000.0
MISSING_FUNCTION = (
    "simulate failed: HostError: Error(WasmVm, MissingValue)\n"
    'Event log: "trying to invoke non-existent contract function", version'
)


def make_plan(
    plan_id: str = "pln_0a1b2c3d", prices: tuple[float, ...] = (0.012, 0.03), created_at: float = NOW
) -> StoredPlan:
    steps = [
        PlanStep(agent_id=f"agt_{i}", rationale="r", est_price_usdc=p, est_eta_seconds=1.0)
        for i, p in enumerate(prices)
    ]
    return StoredPlan(
        id=plan_id, intent="x", plan=Plan(steps=steps), total_usdc=sum(prices), total_eta=1.0, created_at=created_at
    )


def record(now: float = NOW, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "payer": PAYER,
        "agent_id": "pln_0a1b2c3d",
        "max_amount": 420_000,
        "spent": 0,
        "expires_at": int(now) + 3_600,
        "revoked": False,
        "settled": False,
    }
    base.update(overrides)
    return base


class FakeEscrow:
    """Answers `version` and `authorization` the way the RPC would, and counts the reads."""

    def __init__(self, version: int | Exception | None = 2, auth: dict[str, Any] | Exception | None = None) -> None:
        self.version = version
        self.auth: dict[str, Any] | Exception | None = record() if auth is None else auth
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, contract_id: str, function_name: str, args: list[Any] | None = None, source: str | None = None, **_: Any
    ) -> Any:
        self.calls.append((contract_id, function_name))
        answer = self.version if function_name == "version" else self.auth
        if function_name == "version" and answer is None:
            raise RuntimeError(MISSING_FUNCTION)
        if isinstance(answer, Exception):
            raise answer
        return answer


def use_escrow(monkeypatch: pytest.MonkeyPatch, escrow: str) -> None:
    ids = sc.ContractIds(
        agent_registry="", reputation_ledger="", payment_escrow=escrow, attestation_registry="", asset_sac=""
    )
    monkeypatch.setattr(sc, "contract_ids", lambda: ids)


def install(monkeypatch: pytest.MonkeyPatch, escrow: FakeEscrow) -> FakeEscrow:
    monkeypatch.setattr(sc, "simulate_read", escrow)
    return escrow
