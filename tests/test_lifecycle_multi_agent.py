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
from scripts.lifecycle.config import EXIT_PLAN_MISSING_AGENT, EXIT_VERIFY_FAILED, Budgets
from scripts.lifecycle.fakes import AGENT, AGENT_2, AGENT_2_NAME, API, FakeWorld

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


# ── partial delivery: one agent delivers, one stops answering ───────────
AC5_CHECKS = {
    "v2_charged_per_delivered_step",
    "v2_paid_sum_matches_settlement",
    "v2_settled_event",
    "v2_buyer_balance_delta",
    "seal_receipts_match_charges",
    "seal_names_every_agent",
    "seal_receipts_are_delivered_steps",
    f"failed_step_rated_20:{AGENT_2}",
}


@pytest.mark.parametrize(
    ("second_first", "agents"),
    [(False, (AGENT, AGENT_2)), (True, (AGENT_2, AGENT))],
    ids=["timeout-last", "timeout-first-and-named-first"],
)
def test_a_mixed_plan_proves_only_the_delivered_step_was_paid(
    buyer: Keypair, tmp_path: Path, second_first: bool, agents: tuple[str, str]
) -> None:
    world = FakeWorld(buyer=buyer.public_key, second_agent=True, second_first=second_first, undelivered={AGENT_2})

    result = run(world, buyer, tmp_path, *agents)

    assert result.code == 0, result.out
    checks = result.checks()
    assert AC5_CHECKS <= set(checks), sorted(checks)
    assert all(checks[name]["ok"] is True for name in AC5_CHECKS), {n: checks[n] for n in AC5_CHECKS}
    # The chain paid AGENT alone and handed AGENT_2's share back to the buyer.
    price = 500_000
    authorized = 2 * price + 200_000  # two external steps at 0.05, the writer at 0.02
    assert checks["v2_settled_event"]["detail"] == f"spent {price}, returned {authorized - price} to the buyer"
    assert checks["v2_buyer_balance_delta"]["detail"].startswith(f"buyer balance fell {price + 100} stroops")
    assert world.balances[world.owner_2] == 50 * 10_000_000
    [sealed] = world.attestations.values()
    delivered = next(s for s in result.row("settlement_checks")["detail"]["steps"] if s["agent_id"] == AGENT)
    assert sealed["receipts"] == [delivered["receipt_id_hex"]]
    # The failed agent's 20/100 was read back from the ledger, not the trace.
    rated = {r["agent"]: r["detail"]["rating"] for r in result.rows() if r["event"] == "rating"}
    assert rated[AGENT_2] == 20
    # The dispute goes to the step that was paid for, whichever agent was named first.
    assert result.row("dispute_opened")["detail"]["step_index"] == delivered["step_index"]
    assert "[PASS] failed_step_rated_20:ext_faulty" in result.out


def test_an_all_delivered_plan_has_no_failure_to_rate(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, second_agent=True)

    result = run(world, buyer, tmp_path, AGENT, AGENT_2, until="verify")

    assert result.code == 0, result.out
    checks = result.checks()
    assert checks["failed_steps_rated_20"] == {
        "name": "failed_steps_rated_20",
        "ok": True,
        "detail": "every step delivered; no failure to rate",
    }
    assert checks["seal_receipts_are_delivered_steps"]["ok"] is True
    assert checks["v2_charged_per_delivered_step"]["detail"].startswith("2 charged event(s) for 2 paid step(s)")
    assert checks["v2_settled_event"]["detail"] == "spent 1000000, returned 200000 to the buyer"


def test_a_settle_that_paid_the_step_that_timed_out_fails_verification(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, second_agent=True, undelivered={AGENT_2}, pays_undelivered=True)

    result = run(world, buyer, tmp_path, AGENT, AGENT_2)

    assert result.code == EXIT_VERIFY_FAILED, result.out
    checks = result.checks()
    assert checks["v2_charged_per_delivered_step"]["ok"] is False
    assert checks["seal_receipts_are_delivered_steps"]["ok"] is False
    assert "dispute_opened" not in result.events()


def test_a_failed_step_that_was_not_rated_20_fails_verification(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, second_agent=True, undelivered={AGENT_2}, failed_rating=70)

    result = run(world, buyer, tmp_path, AGENT, AGENT_2)

    assert result.code == EXIT_VERIFY_FAILED, result.out
    check = result.checks()[f"failed_step_rated_20:{AGENT_2}"]
    assert check["ok"] is False
    assert check["detail"] == f"1 undelivered step(s); ratings landed for {AGENT_2}: [70]"


def test_a_twenty_the_trace_claims_but_the_ledger_does_not_hold_fails_verification(
    buyer: Keypair, tmp_path: Path
) -> None:
    world = FakeWorld(buyer=buyer.public_key, second_agent=True, undelivered={AGENT_2}, ledger_failed_rating=70)

    result = run(world, buyer, tmp_path, AGENT, AGENT_2)

    assert result.code == EXIT_VERIFY_FAILED, result.out
    # The trace says 20/100; the ledger's `rated` event, which is what counts, says 70.
    [trace] = world.traces.values()
    assert any(line["msg"].startswith(f"reputation → {AGENT_2_NAME} rated 20/100") for line in trace)
    assert result.checks()[f"failed_step_rated_20:{AGENT_2}"]["ok"] is False


def test_a_settle_that_kept_back_part_of_the_remainder_fails_verification(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, second_agent=True, undelivered={AGENT_2}, withheld=1)

    result = run(world, buyer, tmp_path, AGENT, AGENT_2)

    assert result.code == EXIT_VERIFY_FAILED, result.out
    checks = result.checks()
    assert checks["v2_settled_event"]["ok"] is False
    assert "returned 699999 != max 1200000 - spent 500000" in checks["v2_settled_event"]["detail"]
    assert checks["v2_buyer_balance_delta"]["ok"] is False
