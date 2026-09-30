"""The lifecycle harness over a multi-agent plan (story 5.01 AC5).

`--agent` repeats. The plan must route to every named agent or nothing is
signed, and `verify` then proves partial delivery from the chain: the settle
paid only the steps that delivered, returned the rest to the buyer, the seal
carries only those steps' receipts, and every step that failed cost its agent
a landed 20/100.

Driven through `scripts.lifecycle.cli.main` against `FakeWorld` with its
second external agent on, exactly as `test_lifecycle_run.py` drives one.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from stellar_sdk import Keypair

from scripts.lifecycle.cli import main
from scripts.lifecycle.config import EXIT_PLAN_MISSING_AGENT, Budgets
from scripts.lifecycle.fakes import AGENT, AGENT_2, API, FakeWorld

BUDGETS = Budgets(warmup=30, task=60, poll_interval=4, tx_observe=4, refund=30, refund_interval=5)


@dataclass
class Outcome:
    code: int
    out: str
    dir: Path

    def rows(self) -> list[dict[str, Any]]:
        path = self.dir / "lifecycle.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def events(self) -> list[str]:
        return [r["event"] for r in self.rows()]

    def row(self, event: str) -> dict[str, Any]:
        return next(r for r in self.rows() if r["event"] == event)

    def checks(self) -> dict[str, Any]:
        return {c["name"]: c for c in self.row("settlement_checks")["detail"]["checks"]}


@pytest.fixture
def buyer() -> Keypair:
    return Keypair.random()


def run(world: FakeWorld, buyer: Keypair, directory: Path, *agents: str, until: str = "reputation") -> Outcome:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    stream = io.StringIO()
    argv = [
        "--api",
        API,
        *[arg for agent in agents for arg in ("--agent", agent)],
        "--buyer-secret-env",
        "BUYER_1_SECRET",
        "--adjudicator-key-env",
        "ORIZON_API_KEY",
        "--evidence-dir",
        str(directory),
        "--intent",
        "build me a landing page and review it",
        "--until",
        until,
    ]
    code = main(
        argv,
        transport=world.transport(),
        environ={"BUYER_1_SECRET": buyer.secret, "ORIZON_API_KEY": world.adjudicator_key},
        stream=stream,
        sleep=sleep,
        clock=lambda: clock[0],
        budgets=BUDGETS,
    )
    return Outcome(code, stream.getvalue(), directory)


def posts(world: FakeWorld, path: str) -> int:
    return sum(1 for c in world.calls if c["path"] == path and c["method"] == "POST")


# ── the plan must route to every agent ──────────────────────────────────
def test_refuses_a_plan_that_misses_one_of_the_agents(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, second_agent=True, plan_routes_second=False)

    result = run(world, buyer, tmp_path, AGENT, AGENT_2)

    assert result.code == EXIT_PLAN_MISSING_AGENT, result.out
    assert f"not {AGENT_2}; nothing was signed" in result.out
    missing = result.row("plan_missing_agent")
    assert missing["detail"]["missing"] == [AGENT_2]
    assert posts(world, "/api/stellar/build/authorize") == 0
    assert posts(world, "/api/stellar/submit") == 0
    assert posts(world, "/api/orchestrator/execute") == 0


def test_the_order_of_the_agents_does_not_matter(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, second_agent=True)

    result = run(world, buyer, tmp_path, AGENT_2, AGENT, until="decompose")

    assert result.code == 0, result.out
    assert [s["agent_id"] for s in result.row("plan")["detail"]["plan"]["steps"]] == [AGENT, AGENT_2, "agt_writer"]
