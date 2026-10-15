"""Branch-level characterization of the lifecycle runner's sequencing, resume, poll and verify.

The end-to-end suite walks the lifecycle's main roads; these tests pin the
side roads — an interrupt, a resume across the escrow switch, a resume of
another task, a trace line for a rating that never landed, a settlement
with no hash — so the runner's exit codes, evidence rows and console lines
stay exactly what they are.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from stellar_sdk import Keypair

from scripts.lifecycle.cli import main
from scripts.lifecycle.config import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_RESUME_CONFLICT,
    EXIT_VERIFY_FAILED,
    Budgets,
)
from scripts.lifecycle.fakes import AGENT, API, REGISTRY, FakeWorld
from scripts.lifecycle.stages import check_mark

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

    def state(self) -> dict[str, Any]:
        return dict(json.loads((self.dir / "state.json").read_text()))


@pytest.fixture
def buyer() -> Keypair:
    return Keypair.random()


@pytest.fixture
def world(buyer: Keypair) -> FakeWorld:
    return FakeWorld(buyer=buyer.public_key)


def run(
    world: FakeWorld,
    buyer: Keypair,
    directory: Path,
    *extra: str,
    agents: tuple[str, ...] = (AGENT,),
    intent: bool = True,
) -> Outcome:
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
        *(["--intent", "build me a landing page"] if intent else []),
        *extra,
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


def edit_state(directory: Path, **changes: Any) -> None:
    path = directory / "state.json"
    state = json.loads(path.read_text())
    state.update(changes)
    path.write_text(json.dumps(state))


# ── run ─────────────────────────────────────────────────────────
def test_an_interrupt_mid_run_is_recorded_and_re_raised(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    real = world.handle

    def interrupted(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/orchestrator/execute":
            raise KeyboardInterrupt
        return real(request)

    world.handle = interrupted  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        run(world, buyer, tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "lifecycle.jsonl").read_text().splitlines()]
    assert rows[-1]["event"] == "run_interrupted"
    assert rows[-1]["detail"]["summary"] == "interrupted by the operator; state saved"


def test_an_interrupt_in_a_dry_run_records_nothing(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    real = world.handle

    def interrupted(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/stellar/reputation":
            raise KeyboardInterrupt
        return real(request)

    world.handle = interrupted  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        run(world, buyer, tmp_path / "run", "--dry-run")
    assert not (tmp_path / "run").exists()


def test_a_crash_before_a_run_id_records_nothing(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, crash_on="/api/stellar/network")
    with pytest.raises(RuntimeError, match="fake crash on /api/stellar/network"):
        run(world, buyer, tmp_path / "run")
    assert not (tmp_path / "run" / "lifecycle.jsonl").exists()


# ── preflight ───────────────────────────────────────────────────
def test_an_agent_the_api_does_not_list_is_refused(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    result = run(world, buyer, tmp_path, agents=(AGENT, "ghost"))
    assert result.code == EXIT_REFUSED
    assert "STOPPED (exit 3): agent 'ghost' is not listed by GET /api/agents" in result.out


def test_a_resume_across_the_escrow_switch_verifies_the_runs_escrow(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    first = run(world, buyer, tmp_path, "--until", "execute")
    assert first.code == EXIT_OK
    state = first.state()
    edit_state(tmp_path, authorize={**state["authorize"], "escrow": REGISTRY})
    task_id = next(iter(world.tasks))
    resumed = run(world, buyer, tmp_path, "--from-task", task_id, "--until", "poll", intent=False)
    assert resumed.code == EXIT_OK, resumed.out
    assert f"NOTE: this run authorized against {REGISTRY}; the API now names " in resumed.out
    assert "escrow v1 (version() absent: no fake for " in resumed.out
    assert resumed.state()["escrow_version"] == 1


# ── resume_point ────────────────────────────────────────────────
def test_a_resume_with_another_buyers_state_is_refused(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    run(world, buyer, tmp_path, "--until", "execute")
    other = Keypair.random().public_key
    edit_state(tmp_path, buyer=other)
    resumed = run(world, buyer, tmp_path, "--from-task", next(iter(world.tasks)), intent=False)
    assert resumed.code == EXIT_RESUME_CONFLICT
    assert f"STOPPED (exit 9): {tmp_path}/state.json is buyer {other}'s run, not this key's" in resumed.out


def test_a_resume_of_another_task_drops_the_first_tasks_context(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    run(world, buyer, tmp_path / "a", "--until", "verify")
    run(world, buyer, tmp_path / "b", "--until", "execute")
    first, second = list(world.tasks)
    resumed = run(world, buyer, tmp_path / "a", "--from-task", second, "--until", "poll", intent=False)
    assert resumed.code == EXIT_OK, resumed.out
    state = resumed.state()
    assert state["task_id"] == second
    assert first != second
    assert state["read_token"] is None
    assert state["plan"] is None
    assert state["authorize"] is None
    assert state["settlement"] is None
    assert state["dispute"] is None
    assert state["balances_before"] is None
    assert state["owners"] is None


def test_from_dispute_in_a_fresh_directory_resumes_at_the_uphold(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    run(world, buyer, tmp_path / "a", "--until", "dispute")
    dispute_id = next(iter(world.disputes))
    resumed = run(world, buyer, tmp_path / "b", "--from-dispute", dispute_id, "--until", "uphold", intent=False)
    assert resumed.code == EXIT_OK, resumed.out
    assert resumed.state()["dispute"] == {"id": dispute_id, "status": "credited"}
    assert "reputation[resume_at_uphold]" in resumed.out


def test_a_fresh_run_without_an_intent_is_refused(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    result = run(world, buyer, tmp_path, intent=False)
    assert result.code == EXIT_REFUSED
    assert "STOPPED (exit 3): --intent is required for a fresh run" in result.out


# ── print_plan ──────────────────────────────────────────────────
def test_a_dry_run_on_a_network_without_a_sac_reads_no_balance(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    real = world.stellar_network

    def without_sac() -> httpx.Response:
        body = real().json()
        body.pop("asset_sac")
        return httpx.Response(200, json=body)

    world.stellar_network = without_sac  # type: ignore[method-assign]
    result = run(world, buyer, tmp_path / "run", "--dry-run")
    assert result.code == EXIT_OK, result.out
    assert "buyer balance" not in result.out
    assert "does not exist" not in result.out


def test_a_dry_resume_past_decompose_does_not_block_on_an_unfunded_buyer(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    run(world, buyer, tmp_path, "--until", "execute")
    del world.balances[buyer.public_key]
    result = run(world, buyer, tmp_path, "--dry-run", "--from-task", next(iter(world.tasks)), intent=False)
    assert result.code == EXIT_OK, result.out
    assert f"buyer {buyer.public_key} does not exist on testnet: fund it (friendbot) before a real run" in result.out
    assert "A real run with these flags would stop" not in result.out


# ── record_ratings ──────────────────────────────────────────────
def test_a_rating_the_trace_says_never_landed_is_recorded_unresolved(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    run(world, buyer, tmp_path, "--until", "execute")
    task_id = next(iter(world.tasks))
    world.traces[task_id].append(
        {"t": "1.5", "level": "proof", "msg": "reputation submit failed for External Agent: boom · tx 0123456789…"}
    )
    resumed = run(world, buyer, tmp_path, "--from-task", task_id, "--until", "poll", intent=False)
    assert resumed.code == EXIT_OK, resumed.out
    unresolved = [r for r in resumed.rows() if r["event"] == "rating_unresolved"]
    assert [r["detail"] for r in unresolved] == [
        {
            "summary": f"rating for {AGENT} (tx 0123456789…) not found in the ledger's rated events",
            "tx_prefix": "0123456789",
            "landed_per_trace": False,
            "scanned_from_ledger": unresolved[0]["detail"]["scanned_from_ledger"],
        }
    ]
    assert resumed.events().count("rating") == 2


def test_a_run_whose_trace_has_no_rating_records_none(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    world._write_ratings = lambda *_args: None  # type: ignore[method-assign]
    result = run(world, buyer, tmp_path, "--until", "poll")
    assert result.code == EXIT_OK, result.out
    assert "rating" not in result.events()
    assert "rating_unresolved" not in result.events()
    assert "events_unavailable" not in result.events()


def test_a_poll_past_the_event_window_records_every_rating_unresolved(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    run(world, buyer, tmp_path, "--until", "execute")
    world.events_forgotten = True
    resumed = run(world, buyer, tmp_path, "--from-task", next(iter(world.tasks)), "--until", "poll", intent=False)
    assert resumed.code == EXIT_OK, resumed.out
    poll_rows = [r for r in resumed.rows() if r["stage"] == "poll"]
    assert [r["event"] for r in poll_rows] == [
        "task_terminal",
        "events_unavailable",
        "rating_unresolved",
        "rating_unresolved",
    ]
    assert poll_rows[1]["detail"]["summary"].startswith("getEvents refused: getEvents: ")


# ── stage_verify ────────────────────────────────────────────────
def test_a_settlement_without_hashes_fails_verification_plainly(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    run(world, buyer, tmp_path, "--until", "poll")
    task_id = next(iter(world.tasks))
    world.settlements[task_id].update(charge_tx=None, proof_tx=None)
    edit_state(tmp_path, settlement=None, owners=None, authorize=None)
    resumed = run(world, buyer, tmp_path, "--from-task", task_id, "--until", "verify", intent=False)
    assert resumed.code == EXIT_VERIFY_FAILED
    assert "settle" not in resumed.events() and "seal" not in resumed.events()
    checks = {c["name"]: c for c in resumed.rows()[-2]["detail"]["checks"]}
    assert checks["v2_settle_tx"] == {
        "name": "v2_settle_tx",
        "ok": False,
        "detail": "no transaction hash was recorded",
    }
    assert checks["seal_tx"]["detail"] == "no transaction hash was recorded"
    assert "v2_authorization_view" not in checks
    assert resumed.state()["owners"] == {AGENT: world.owner}


def test_a_settle_hash_the_ledger_never_saw_reads_no_events(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    run(world, buyer, tmp_path, "--until", "poll")
    task_id = next(iter(world.tasks))
    world.settlements[task_id]["charge_tx"] = "ff" * 32
    edit_state(tmp_path, settlement=None)
    resumed = run(world, buyer, tmp_path, "--from-task", task_id, "--until", "verify", intent=False)
    assert resumed.code == EXIT_VERIFY_FAILED
    settle = next(r for r in resumed.rows() if r["event"] == "settle")
    assert settle["tx_hash"] == "ff" * 32 and settle["onchain_status"] == "NOT_FOUND"
    assert "events_unavailable" not in resumed.events()


@pytest.mark.parametrize(("ok", "mark"), [(True, "PASS"), (False, "FAIL"), (None, "n/a ")])
def test_a_settlement_check_prints_a_four_character_mark(ok: bool | None, mark: str) -> None:
    assert check_mark(ok) == mark
