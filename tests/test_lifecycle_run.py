"""The lifecycle harness end to end, against the in-memory world (story 5.01).

Every test drives `scripts.lifecycle.cli.main` exactly as an operator would,
with `FakeWorld` answering the API, the RPC and Horizon. The world is honest
enough to fail a harness that signs the wrong thing: a submit needs a valid
buyer signature, a dispute needs a SEP-53 signature by the payer, and v2
custody moves balances the way the interface says.
"""

from __future__ import annotations

import io
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from stellar_sdk import Keypair

from scripts.lifecycle.cli import main
from scripts.lifecycle.config import (
    EXIT_OK,
    EXIT_PLAN_MISSING_AGENT,
    EXIT_REFUSED,
    EXIT_RESUME_CONFLICT,
    EXIT_STAGE_FAILED,
    EXIT_TIMED_OUT,
    EXIT_UNKNOWN_OUTCOME,
    EXIT_VERIFY_FAILED,
    Budgets,
)
from scripts.lifecycle.fakes import AGENT, API, ESCROW, MAINNET_PASSPHRASE, FakeWorld

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


@pytest.fixture
def buyer() -> Keypair:
    return Keypair.random()


@pytest.fixture
def world(buyer: Keypair) -> FakeWorld:
    return FakeWorld(buyer=buyer.public_key)


def run(world: FakeWorld, buyer: Keypair, directory: Path, *extra: str, intent: bool = True) -> Outcome:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    stream = io.StringIO()
    argv = [
        "--api",
        API,
        "--agent",
        AGENT,
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


def posts(world: FakeWorld, path: str) -> int:
    return sum(1 for c in world.calls if c["path"] == path and c["method"] == "POST")


# ── the whole lifecycle ─────────────────────────────────────────
def test_v2_lifecycle_records_every_hash_with_the_ledgers_status(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    result = run(world, buyer, tmp_path)

    assert result.code == EXIT_OK, result.out
    tx_rows = [r for r in result.rows() if r.get("tx_hash")]
    assert [r["event"] for r in tx_rows] == [
        "authorize",
        "rating",
        "rating",
        "settle",
        "seal",
        "refund",
        "dispute_rating",
    ]
    for r in tx_rows:
        # read back from the fake ledger, never assumed
        assert r["onchain_status"] == world.txs[r["tx_hash"]]["status"] == "SUCCESS"
        assert r["explorer"] == f"https://stellar.expert/explorer/testnet/tx/{r['tx_hash']}"
        assert r["network"] == "testnet"
        assert r["buyer"] == buyer.public_key
        assert r["contract"]
    assert result.row("settle")["contract"] == ESCROW
    assert result.row("refund")["amount"] == "0.0500000"
    checks = result.row("settlement_checks")["detail"]["checks"]
    assert all(c["ok"] is not False for c in checks)
    assert {c["name"] for c in checks} >= {
        "v2_charged_per_delivered_step",
        "v2_settled_event",
        "v2_authorization_view",
        "v2_buyer_balance_delta",
        "seal_on_registry",
    }
    md = (tmp_path / "lifecycle.md").read_text()
    assert "## Transactions" in md and "## Reputation snapshots" in md
    assert result.row("settle")["tx_hash"][:8] in md


def test_the_authorize_is_labelled_with_the_plan_id_on_v2(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    run(world, buyer, tmp_path)
    body = world.call_log("/api/stellar/build/authorize")[0]["json"]
    assert body == {"payer": buyer.public_key, "agent_id": "pln_0a1b2c3d", "max_amount_usdc": 0.07, "ttl_seconds": 1800}


def test_v1_authorizes_the_batch_label_the_console_sends(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, escrow_version=1)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_OK, result.out
    body = world.call_log("/api/stellar/build/authorize")[0]["json"]
    assert body["agent_id"] == "orizon_batch" and body["ttl_seconds"] == 600
    names = {c["name"] for c in result.row("settlement_checks")["detail"]["checks"]}
    assert "v1_charge_tx" in names and "v1_charged_event" in names


def test_v1_charge_that_failed_on_the_ledger_stops_before_any_dispute(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, escrow_version=1, charge_succeeds=False)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_VERIFY_FAILED
    assert "D-039" in result.out
    assert posts(world, "/api/disputes/challenge") == 0


def test_execute_carries_the_auth_id_and_payer_from_the_authorize_result(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    run(world, buyer, tmp_path)
    body = world.call_log("/api/orchestrator/execute")[0]["json"]
    assert body["payer"] == buyer.public_key
    assert body["auth_id_hex"] in world.authorizations
    assert body["plan_id"] == "pln_0a1b2c3d"


def test_task_reads_send_the_read_token(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    run(world, buyer, tmp_path)
    reads = [c for c in world.calls if c["path"].startswith(("/api/tasks/", "/api/trace/"))]
    assert reads
    token = next(iter(world.tokens.values()))
    assert all(c["headers"].get("x-task-token") == token for c in reads)


def test_the_uphold_carries_the_adjudicator_key(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    run(world, buyer, tmp_path)
    (call,) = [c for c in world.calls if c["path"].endswith("/uphold")]
    assert call["headers"]["x-api-key"] == world.adjudicator_key


def test_dispute_reads_send_the_read_grant(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    run(world, buyer, tmp_path)
    reads = [c for c in world.calls if c["path"].startswith("/api/disputes/dsp_") and c["method"] == "GET"]
    assert reads and all(c["headers"].get("x-dispute-read-grant") in world.grants for c in reads)


def test_a_backend_without_the_read_grant_routes_still_completes(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, d067=False)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_OK, result.out
    assert "no read grant" in result.out


def test_reputation_is_snapshotted_at_each_stage(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    result = run(world, buyer, tmp_path)
    labels = [r["detail"]["label"] for r in result.rows() if r["event"] == "reputation_snapshot"]
    assert labels == ["start", "after_rating_1", "after_ratings", "after_dispute", "final"]
    summary = result.row("reputation_summary")["detail"]
    assert summary["moved_start_to_ratings"] is True
    assert summary["moved_ratings_to_dispute"] is True
    assert summary["fell_after_dispute"] is True
    assert (summary["source_start"], summary["source_final"]) == ("prior", "onchain")


# ── refusals before anything is signed ──────────────────────────
def test_refuses_a_mainnet_api(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, passphrase=MAINNET_PASSPHRASE)
    result = run(world, buyer, tmp_path / "run")
    assert result.code == EXIT_REFUSED
    assert "testnet only" in result.out
    assert not any(c["method"] == "POST" and c["path"].startswith("/api/") for c in world.calls)
    assert not (tmp_path / "run").exists()


def test_refuses_an_rpc_on_another_network(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, rpc_passphrase=MAINNET_PASSPHRASE)
    result = run(world, buyer, tmp_path / "run")
    assert result.code == EXIT_REFUSED
    assert not any(c["method"] == "POST" and c["path"].startswith("/api/") for c in world.calls)


def test_refuses_a_plan_that_does_not_route_to_the_agent(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, plan_routes_agent=False)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_PLAN_MISSING_AGENT
    assert posts(world, "/api/stellar/build/authorize") == 0
    assert "plan_missing_agent" in result.events()


def test_refuses_an_agent_that_is_not_bound(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, agent_bound=False)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_REFUSED
    assert posts(world, "/api/orchestrator/decompose") == 0


def test_refuses_to_sign_an_envelope_for_another_contract(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, wrong_escrow_in_xdr=True)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_STAGE_FAILED
    assert "refusing to sign" in result.out
    assert posts(world, "/api/stellar/submit") == 0


def test_refuses_an_unfunded_buyer_before_anything_is_signed(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key)
    del world.balances[buyer.public_key]
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_REFUSED
    assert "friendbot" in result.out
    assert posts(world, "/api/stellar/build/authorize") == 0


def test_a_dry_run_names_an_unfunded_buyer(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key)
    del world.balances[buyer.public_key]
    result = run(world, buyer, tmp_path / "run", "--dry-run")
    assert result.code == EXIT_OK
    assert "does not exist on testnet" in result.out


def test_refuses_without_the_buyer_secret(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    stream = io.StringIO()
    code = main(
        [
            "--api",
            API,
            "--agent",
            AGENT,
            "--intent",
            "x y z",
            "--buyer-secret-env",
            "NOPE",
            "--evidence-dir",
            str(tmp_path),
        ],
        transport=world.transport(),
        environ={},
        stream=stream,
        sleep=lambda s: None,
        budgets=BUDGETS,
    )
    assert code == EXIT_REFUSED
    assert "$NOPE is not set" in stream.getvalue()


def test_refuses_a_seed_pasted_where_a_variable_name_belongs(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    stream = io.StringIO()
    code = main(
        ["--api", API, "--agent", AGENT, "--buyer-secret-env", buyer.secret, "--evidence-dir", str(tmp_path)],
        transport=world.transport(),
        environ={},
        stream=stream,
    )
    assert code == EXIT_REFUSED
    assert buyer.secret not in stream.getvalue()
    assert world.calls == []


# ── secrets ─────────────────────────────────────────────────────
def test_never_prints_or_records_a_secret(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_OK
    signed = world.call_log("/api/stellar/submit")[0]["json"]["signed_xdr"]
    token = next(iter(world.tokens.values()))
    signatures = [c["json"]["signature_b64"] for c in world.calls if c["path"] == "/api/disputes"]
    evidence = (tmp_path / "lifecycle.jsonl").read_text() + (tmp_path / "lifecycle.md").read_text()
    for secret in [buyer.secret, world.adjudicator_key, signed, token, *signatures, *world.grants]:
        assert secret not in result.out
        assert secret not in evidence
    state = (tmp_path / "state.json").read_text()
    assert buyer.secret not in state and world.adjudicator_key not in state and signed not in state
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600


# ── crash and unknown outcomes ──────────────────────────────────
def test_evidence_survives_a_crash_mid_run(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    world.crash_on = "/api/disputes/challenge"
    with pytest.raises(RuntimeError, match="fake crash"):
        run(world, buyer, tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "lifecycle.jsonl").read_text().splitlines()]
    events = [r["event"] for r in rows]
    assert {"authorize", "settle", "seal"} <= set(events)
    assert events[-1] == "run_crashed"
    md = (tmp_path / "lifecycle.md").read_text()
    assert next(r for r in rows if r["event"] == "seal")["tx_hash"][:8] in md


def test_a_lost_submit_is_read_back_and_never_resent(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, submit_mode="transport", submit_lands=True)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_UNKNOWN_OUTCOME
    assert posts(world, "/api/stellar/submit") == 1
    row = result.row("authorize_unknown")
    assert row["onchain_status"] == "SUCCESS"  # it landed; the ledger says so
    assert posts(world, "/api/orchestrator/execute") == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["authorize"]["status"] == "unknown"


def test_a_lost_submit_that_never_landed_reads_not_found(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, submit_mode="submit_failed", submit_lands=False)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_UNKNOWN_OUTCOME
    assert result.row("authorize_unknown")["onchain_status"] == "NOT_FOUND"
    assert posts(world, "/api/stellar/submit") == 1


def test_a_submit_the_backend_lost_track_of_is_unknown(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, submit_mode="timeout_status")
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_UNKNOWN_OUTCOME
    assert posts(world, "/api/stellar/submit") == 1


def test_a_failed_authorize_stops_with_the_ledgers_status(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, submit_mode="api_failed_status")
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_STAGE_FAILED
    assert result.row("authorize")["onchain_status"] == "FAILED"


def test_a_lost_execute_is_never_resent(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, execute_mode="transport")
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_UNKNOWN_OUTCOME
    assert posts(world, "/api/orchestrator/execute") == 1


def test_an_execute_refused_for_capacity_is_a_definite_failure(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, execute_mode="capacity")
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_STAGE_FAILED
    assert posts(world, "/api/orchestrator/execute") == 1


def test_a_lost_uphold_is_read_back_and_never_resent(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, uphold_mode="transport", credit_stuck=True)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_UNKNOWN_OUTCOME
    assert posts(world, next(c["path"] for c in world.calls if c["path"].endswith("/uphold"))) == 1
    assert result.row("uphold_unknown")["detail"]["status"] == "crediting"


def test_an_unconfirmed_refund_is_unknown_and_its_hash_is_read(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, uphold_mode="unconfirmed", credit_stuck=True)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_UNKNOWN_OUTCOME
    refund = result.row("refund")
    assert refund["onchain_status"] == "NOT_FOUND"


def test_a_refused_uphold_stops(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, uphold_mode="refused")
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_STAGE_FAILED
    assert "uphold_refused" in result.events()


def test_a_credit_that_never_lands_times_out(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, refund_lags=10_000)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_TIMED_OUT
    assert "refund_timeout" in result.events()


def test_a_slow_credit_is_waited_for(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, refund_lags=3)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_OK, result.out


# ── the dispute's own paths ─────────────────────────────────────
def test_an_expired_challenge_is_retried_once(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, open_dispute_mode="expired_once")
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_OK, result.out
    assert posts(world, "/api/disputes") == 2


def test_a_lost_dispute_open_is_found_by_a_read(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, open_dispute_mode="transport")
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_OK, result.out
    assert posts(world, "/api/disputes") == 1
    assert len(world.disputes) == 1


def test_the_dispute_is_signed_sep53_by_the_payer_over_the_servers_message(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    import base64

    run(world, buyer, tmp_path)
    challenge_call = world.call_log("/api/disputes/challenge")[0]["json"]
    body = world.call_log("/api/disputes")[0]["json"]
    message = f"orizon-dispute:v1:{challenge_call['job_id_hex']}:{challenge_call['step_index']}:{body['nonce']}"
    Keypair.from_public_key(buyer.public_key).verify_message(message, base64.b64decode(body["signature_b64"]))
    assert body["payer"] == buyer.public_key and body["step_index"] == 0


# ── the settlement checks ───────────────────────────────────────
def test_a_payout_that_does_not_match_its_event_fails_verification(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, tamper_payout=True)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_VERIFY_FAILED
    failed = [c for c in result.row("settlement_checks")["detail"]["checks"] if c["ok"] is False]
    assert [c["name"] for c in failed] == [f"v2_operator_delta:{world.owner[:6]}…{world.owner[-4:]}"]
    assert posts(world, "/api/disputes/challenge") == 0


def test_a_step_that_did_not_deliver_is_not_charged_and_not_disputable(buyer: Keypair, tmp_path: Path) -> None:
    """AC5: the external agent stopped answering. The run seals, pays nothing
    for the dead step, and the harness will not dispute a step nobody paid for."""
    world = FakeWorld(buyer=buyer.public_key, undelivered={AGENT})
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_STAGE_FAILED
    checks = {c["name"]: c for c in result.row("settlement_checks")["detail"]["checks"]}
    assert checks["v2_charged_per_delivered_step"]["ok"] is True
    assert "0 charged event(s)" in checks["v2_charged_per_delivered_step"]["detail"]
    assert checks["seal_on_registry"]["ok"] is True
    assert "nothing_to_dispute" in result.events()


# ── stages, resume and reruns ───────────────────────────────────
def test_until_stops_after_the_named_stage(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    result = run(world, buyer, tmp_path, "--until", "verify")
    assert result.code == EXIT_OK
    assert posts(world, "/api/disputes/challenge") == 0
    assert json.loads((tmp_path / "state.json").read_text())["completed"][-1] == "verify"


def test_a_fresh_run_refuses_a_directory_that_holds_one(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    run(world, buyer, tmp_path, "--until", "execute")
    before = posts(world, "/api/stellar/submit")
    again = run(world, buyer, tmp_path)
    assert again.code == EXIT_RESUME_CONFLICT
    assert posts(world, "/api/stellar/submit") == before
    assert posts(world, "/api/orchestrator/decompose") == 1


def test_an_unconfirmed_authorize_blocks_a_fresh_run(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, submit_mode="transport")
    run(world, buyer, tmp_path)
    again = run(world, buyer, tmp_path)
    assert again.code == EXIT_RESUME_CONFLICT
    assert "unconfirmed" in again.out
    assert posts(world, "/api/stellar/submit") == 1


def test_a_restart_between_seal_and_dispute_does_not_lose_the_dispute(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    """AC4: settle, the backend restarts (forgetting the task and its token),
    then the buyer disputes from the settlement record that survived."""
    first = run(world, buyer, tmp_path, "--until", "verify")
    assert first.code == EXIT_OK
    task_id = next(iter(world.tasks))
    world.restarted = True
    world.task_auth_required = False  # production default: the listing is open
    resumed = run(world, buyer, tmp_path, "--from-task", task_id, intent=False)
    assert resumed.code == EXIT_OK, resumed.out
    assert "task_not_in_memory" in resumed.events()
    assert posts(world, "/api/stellar/submit") == 1  # nothing re-paid
    assert len(world.disputes) == 1
    assert resumed.row("refund")["onchain_status"] == "SUCCESS"


def test_resume_uses_the_saved_settlement_when_the_listing_is_gated(
    world: FakeWorld, buyer: Keypair, tmp_path: Path
) -> None:
    run(world, buyer, tmp_path, "--until", "verify")
    task_id = next(iter(world.tasks))
    world.restarted = True  # the token is dead and TASK_AUTH_REQUIRED stays on
    resumed = run(world, buyer, tmp_path, "--from-task", task_id, intent=False)
    assert resumed.code == EXIT_OK, resumed.out
    assert len(world.disputes) == 1


def test_from_dispute_resumes_at_the_uphold(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key)
    run(world, buyer, tmp_path, "--until", "dispute")
    dispute_id = next(iter(world.disputes))
    resumed = run(world, buyer, tmp_path, "--from-dispute", dispute_id, intent=False)
    assert resumed.code == EXIT_OK, resumed.out
    assert posts(world, "/api/stellar/submit") == 1
    assert world.disputes[dispute_id]["status"] == "credited"


def test_from_dispute_never_re_upholds_a_dispute_in_flight(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, uphold_mode="transport", credit_stuck=True)
    run(world, buyer, tmp_path)
    dispute_id = next(iter(world.disputes))
    world.uphold_mode = "ok"
    resumed = run(world, buyer, tmp_path, "--from-dispute", dispute_id, intent=False)
    assert resumed.code == EXIT_UNKNOWN_OUTCOME
    assert "crediting" in resumed.out
    assert sum(1 for c in world.calls if c["path"].endswith("/uphold")) == 1


def test_from_dispute_records_a_credit_that_landed_without_upholding_again(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, uphold_mode="transport", credit_stuck=True)
    run(world, buyer, tmp_path)
    dispute_id = next(iter(world.disputes))
    world.credit_stuck = False  # the in-flight transfer landed
    resumed = run(world, buyer, tmp_path, "--from-dispute", dispute_id, intent=False)
    assert resumed.code == EXIT_OK, resumed.out
    assert "uphold_skipped" in resumed.events()
    assert resumed.row("refund")["onchain_status"] == "SUCCESS"
    assert sum(1 for c in world.calls if c["path"].endswith("/uphold")) == 1


def test_a_resume_for_another_agent_is_refused(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    run(world, buyer, tmp_path, "--until", "verify")
    state = json.loads((tmp_path / "state.json").read_text())
    state["agent"] = "someone_else"
    (tmp_path / "state.json").write_text(json.dumps(state))
    resumed = run(world, buyer, tmp_path, "--from-task", next(iter(world.tasks)), intent=False)
    assert resumed.code == EXIT_RESUME_CONFLICT


def test_evidence_is_appended_across_a_resume(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    first = run(world, buyer, tmp_path, "--until", "verify")
    n = len(first.rows())
    run(world, buyer, tmp_path, "--from-task", next(iter(world.tasks)), intent=False)
    rows = first.rows()
    assert len(rows) > n
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert len({r["run_id"] for r in rows}) == 1


# ── dry run and warm-up ─────────────────────────────────────────
def test_dry_run_reads_and_signs_nothing(world: FakeWorld, buyer: Keypair, tmp_path: Path) -> None:
    result = run(world, buyer, tmp_path / "run", "--dry-run")
    assert result.code == EXIT_OK
    assert "DRY RUN" in result.out and "POST /api/stellar/submit" in result.out
    assert not [c for c in world.calls if c["method"] == "POST" and not c["path"] == "/"]
    assert not (tmp_path / "run").exists()


def test_warms_a_sleeping_backend_before_the_first_call(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, health_failures=3)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_OK
    assert [c["path"] for c in world.calls[:4]] == ["/api/health"] * 4


def test_a_backend_that_never_wakes_is_a_refusal(buyer: Keypair, tmp_path: Path) -> None:
    world = FakeWorld(buyer=buyer.public_key, health_failures=10_000)
    result = run(world, buyer, tmp_path)
    assert result.code == EXIT_REFUSED
    assert "did not wake" in result.out
