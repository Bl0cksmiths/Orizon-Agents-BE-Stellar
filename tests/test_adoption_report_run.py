"""The adoption report end to end, against an in-memory API, RPC and Horizon (story 5.02).

Every test drives `scripts.adoption_report.cli.main` exactly as an operator
would. The world's chain is honest; each test makes the API tell one lie and
asserts the verifier catches it — with a non-zero exit and a message that
names the claim — or, for the true world, that it exits 0.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.adoption_report.cli import main
from scripts.adoption_report.config import (
    EXIT_API_UNREADABLE,
    EXIT_CHAIN_UNREADABLE,
    EXIT_CLAIM_FAILED,
    EXIT_COUNT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_TARGET_NOT_MET,
    usdc_to_stroops,
)
from scripts.adoption_report.fakes import (
    API,
    BUYER,
    ESCROW,
    HORIZON,
    JOB_A,
    MAINNET_PASSPHRASE,
    OP1,
    OP2,
    OTHER_CONTRACT,
    REGISTRY,
    RPC,
    TEAM,
    FakeWorld,
    healthy_world,
)


@dataclass
class Outcome:
    code: int
    out: str
    dir: Path

    def report(self) -> dict[str, Any]:
        return json.loads((self.dir / "adoption-report.json").read_text())

    def fails(self) -> list[str]:
        return [line for line in self.out.splitlines() if line.startswith("FAIL: ")]


def run(world: FakeWorld, tmp_path: Path, *extra: str, register: Path | None = None) -> Outcome:
    stream = io.StringIO()
    out_dir = tmp_path / "evidence"
    reg = register or FakeWorld.write_register(tmp_path / "team_wallets.json")
    code = main(
        [
            "--api",
            API,
            "--rpc-url",
            RPC,
            "--horizon-url",
            HORIZON,
            "--escrow",
            ESCROW,
            "--registry",
            REGISTRY,
            "--team-register",
            str(reg),
            "--out-dir",
            str(out_dir),
            *extra,
        ],
        transport=world.transport(),
        stream=stream,
        sleep=lambda _s: None,
        now=lambda: 1_790_000_200.0,
    )
    return Outcome(code, stream.getvalue(), out_dir)


def wf(world: FakeWorld, op: int = 0, agent: int = 0, index: int = 0) -> dict[str, Any]:
    return world.payload["operators"][op]["agents"][agent]["settled_workflows"][index]


# ── the true world ──────────────────────────────────────────────
def test_all_verified_exits_zero(tmp_path: Path) -> None:
    out = run(healthy_world(), tmp_path)
    assert out.code == EXIT_OK, out.out
    assert out.fails() == []
    assert "exit 0: VERIFIED" in out.out
    for line in (
        "- **Externally operated agents: 2 of 2 — MET**",
        "- **Unique operator wallets: 2 of 2 — MET**",
        "- **Workflows routed to external agents and settled: 3 of 3 — MET**",
    ):
        assert line in out.out
    report = out.report()
    assert report["totals"] == {"external_agents": 2, "unique_operator_wallets": 2, "settled_external_workflows": 3}
    assert report["verdict"].startswith("VERIFIED")


def test_only_reads_are_made(tmp_path: Path) -> None:
    world = healthy_world()
    run(world, tmp_path)
    methods = {c.split(" ")[1] for c in world.calls if c.startswith("rpc ")}
    assert methods == {"getNetwork", "getTransaction", "simulateTransaction"}


# ── forged owners ───────────────────────────────────────────────
def test_owner_that_does_not_own_the_agent_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.registry["beta"]["owner"] = OP1  # on chain, OP1 owns beta; the API says OP2 does
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert f"FAIL: agent beta — owner_of: AgentRegistry.owner_of(beta) is {OP1}, not the claimed owner {OP2}" in (
        out.fails()
    )


def test_agent_the_registry_does_not_hold_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["operators"][1]["agents"].append(FakeWorld.agent("ghost", []))
    world.payload["totals"]["external_agents"] = 3
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any("owner_of(ghost) failed" in f and "the registry holds no such agent" in f for f in out.fails())


def test_owner_account_missing_from_horizon_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.accounts.discard(OP2)
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert f"FAIL: owner {OP2} — owner_exists: no such account on testnet Horizon" in out.fails()


def test_active_flag_is_checked_against_the_registry(tmp_path: Path) -> None:
    world = healthy_world()
    world.registry["alpha"]["active"] = False
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert "FAIL: agent alpha — active: AgentRegistry.get(alpha).active is False; the API claims True" in out.fails()


# ── team wallets ────────────────────────────────────────────────
def test_team_wallet_claimed_as_external_fails(tmp_path: Path) -> None:
    world = healthy_world()
    register = FakeWorld.write_register(tmp_path / "team.json", [TEAM, OP2])
    out = run(world, tmp_path, register=register)
    assert out.code == EXIT_CLAIM_FAILED
    assert any(
        f.startswith(f"FAIL: owner {OP2} — not_team_wallet: a team wallet: {OP2} is in the team register")
        for f in out.fails()
    )
    # and it is not counted, so the recount shows the target NOT MET
    assert "- **Unique operator wallets: 1 of 2 — NOT MET**" in out.out


def test_an_account_the_register_only_cites_as_evidence_is_not_ours(tmp_path: Path) -> None:
    """An entry's `evidence` may name other accounts — the friendbot that
    funded a key, say. Only declared `address`es are team accounts, so an
    operator the evidence happens to mention still verifies as external."""
    register = tmp_path / "cited.json"
    register.write_text(
        json.dumps({"wallets": [{"address": TEAM, "role": "team", "evidence": f"funded by {OP1} via friendbot"}]})
    )
    out = run(healthy_world(), tmp_path, register=register)
    assert out.code == EXIT_OK
    assert out.report()["team_register"]["accounts"] == 1


@pytest.mark.parametrize(
    "body",
    [
        {"team": [TEAM]},
        {"wallets": {"address": TEAM}},
        {"wallets": [{"role": "team"}]},
        {"wallets": [{"address": "GNOTAKEY"}]},
        {"wallets": [TEAM]},
    ],
    ids=["no-wallets-list", "wallets-not-a-list", "entry-without-address", "invalid-address", "bare-string-entry"],
)
def test_a_register_outside_the_committed_layout_is_refused(tmp_path: Path, body: dict) -> None:
    register = tmp_path / "odd.json"
    register.write_text(json.dumps(body))
    out = run(healthy_world(), tmp_path, register=register)
    assert out.code == EXIT_REFUSED


def test_the_committed_register_loads_with_every_declared_wallet() -> None:
    from scripts.adoption_report.register import load

    committed = Path(__file__).resolve().parents[1] / "app" / "data" / "team_wallets.json"
    declared = {entry["address"] for entry in json.loads(committed.read_text())["wallets"]}
    assert load(committed).accounts == frozenset(declared)
    assert len(declared) == 12


def test_empty_or_missing_register_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"wallets": []}))
    out = run(healthy_world(), tmp_path, register=empty)
    assert out.code == EXIT_REFUSED
    assert "names no Stellar account" in out.out
    out = run(healthy_world(), tmp_path, register=tmp_path / "nope.json")
    assert out.code == EXIT_REFUSED
    assert "cannot be read" in out.out


# ── settled workflows ───────────────────────────────────────────
def test_failed_transaction_fails(tmp_path: Path) -> None:
    world = healthy_world()
    bad = world.settle("beta", usdc_to_stroops(0.01), "d4" * 16, status="FAILED")
    wf(world, op=1).update(FakeWorld.workflow(bad, "d4" * 16, 0.01))
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    ledger = world.txs[bad].ledger
    assert f"FAIL: workflow {bad[:6]}…{bad[-4:]} (beta) — tx_success: tx {bad} is FAILED on ledger {ledger} " in (
        "\n".join(out.fails()) + " "
    )


def test_transaction_that_is_not_on_the_ledger_fails(tmp_path: Path) -> None:
    world = healthy_world()
    missing = "ee" * 32
    wf(world, op=1).update(FakeWorld.workflow(missing, "e5" * 16, 0.01))
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any(f"tx {missing} is not on testnet" in f for f in out.fails())


def test_transaction_on_the_wrong_contract_fails(tmp_path: Path) -> None:
    world = healthy_world()
    elsewhere = world.settle("beta", usdc_to_stroops(0.01), "f6" * 16, contract=OTHER_CONTRACT)
    wf(world, op=1).update(FakeWorld.workflow(elsewhere, "f6" * 16, 0.01))
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any(
        f"invokes_escrow: tx {elsewhere} invokes ['{OTHER_CONTRACT}.settle'], not the escrow {ESCROW}" in f
        for f in out.fails()
    )


def test_charged_event_naming_another_agent_fails(tmp_path: Path) -> None:
    world = healthy_world()
    other = world.settle("beta", usdc_to_stroops(0.01), "a7" * 16, charged_agent="gamma")
    wf(world, op=1).update(FakeWorld.workflow(other, "a7" * 16, 0.01))
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any("naming ['gamma']; none names agent beta" in f for f in out.fails())


def test_charged_amount_or_job_that_differs_from_the_claim_fails(tmp_path: Path) -> None:
    world = healthy_world()
    wf(world)["amount_usdc"] = 0.02
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any(
        f"carry (stroops, job) [(100000, '{JOB_A}')]; the API claims (200000, {JOB_A})" in f for f in out.fails()
    )


def test_payer_is_read_back_from_the_authorization(tmp_path: Path) -> None:
    world = healthy_world()
    wf(world)["payer"] = TEAM
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any(f").payer is {BUYER}, not the claimed payer {TEAM}" in f for f in out.fails())


def test_self_settled_workflow_is_not_adoption(tmp_path: Path) -> None:
    world = healthy_world()
    own = world.settle("beta", usdc_to_stroops(0.01), "b8" * 16, payer=OP2)
    wf(world, op=1).update(FakeWorld.workflow(own, "b8" * 16, 0.01, payer=OP2))
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any(f"self-settled: the payer {OP2} is the agent's own owner" in f for f in out.fails())


def test_transaction_past_the_rpc_window_is_proved_by_its_settle_payout(tmp_path: Path) -> None:
    world = healthy_world()
    for tx in world.txs.values():
        tx.rpc_visible = False
    out = run(world, tmp_path)
    assert out.code == EXIT_OK, out.out
    proofs = {w["proof"] for op in out.report()["operators"] for a in op["agents"] for w in a["settled_workflows"]}
    assert proofs == {"settle payout (events past RPC retention)"}


def test_old_transaction_whose_invocation_names_no_payout_fails(tmp_path: Path) -> None:
    world = healthy_world()
    charge = world.settle("beta", usdc_to_stroops(0.01), "c9" * 16, function="charge", rpc_visible=False)
    wf(world, op=1).update(FakeWorld.workflow(charge, "c9" * 16, 0.01))
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any("past the RPC's event window" in f and "the payment to beta cannot be shown" in f for f in out.fails())


def test_explorer_link_that_is_not_testnet_fails(tmp_path: Path) -> None:
    world = healthy_world()
    link = wf(world)["explorer"]
    wf(world)["explorer"] = link.replace("/testnet/", "/public/")
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any("explorer_link" in f and "/public/" in f for f in out.fails())


# ── counts ──────────────────────────────────────────────────────
def test_inflated_total_is_a_count_mismatch(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["totals"]["settled_external_workflows"] = 4
    out = run(world, tmp_path)
    assert out.code == EXIT_COUNT_MISMATCH
    assert out.fails() == ["FAIL: totals — total:settled_external_workflows: API says 4, the verified recount is 3"]


def test_met_flag_the_recount_does_not_support_is_a_count_mismatch(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["operators"][1]["agents"][0]["settled_workflows"] = []
    world.payload["totals"]["settled_external_workflows"] = 2  # honest total, dishonest flag
    out = run(world, tmp_path)
    assert out.code == EXIT_COUNT_MISMATCH
    assert out.fails() == [
        "FAIL: met — met:settled_external_workflows: API says met=True; the recount (2 of 3) is NOT MET"
    ]


def test_same_job_listed_twice_counts_once(tmp_path: Path) -> None:
    world = healthy_world()
    wf(world, index=1).update(wf(world, index=0))
    out = run(world, tmp_path)
    assert out.code == EXIT_COUNT_MISMATCH
    assert "FAIL: totals — total:settled_external_workflows: API says 3, the verified recount is 2" in out.fails()


def test_lowered_target_is_caught(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["targets"]["settled_external_workflows"] = 1
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert "FAIL: targets — target:settled_external_workflows: API target 1; SOW §6.3 target 3" in out.fails()


def test_not_met_is_printed_plainly_and_exits_zero_unless_required(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["operators"] = world.payload["operators"][:1]
    world.payload["totals"] = {"external_agents": 1, "unique_operator_wallets": 1, "settled_external_workflows": 2}
    world.payload["met"] = {
        "external_agents": False,
        "unique_operator_wallets": False,
        "settled_external_workflows": False,
    }
    out = run(world, tmp_path)
    assert out.code == EXIT_OK, out.out
    assert "- **Externally operated agents: 1 of 2 — NOT MET**" in out.out
    assert "- **Workflows routed to external agents and settled: 2 of 3 — NOT MET**" in out.out
    assert run(world, tmp_path, "--require-met").code == EXIT_TARGET_NOT_MET


def test_agent_claimed_external_and_excluded_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["excluded"][0]["agent_ids"].append("beta")
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert "FAIL: agent beta — not_excluded: claimed as external AND listed as excluded" in out.fails()


def test_unreadable_agents_without_the_degraded_flag_fail(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["unreadable_agents"] = ["delta"]
    out = run(world, tmp_path)
    assert out.code == EXIT_CLAIM_FAILED
    assert any("lists 1 unreadable agent(s) but says degraded=false" in f for f in out.fails())
    world.payload["degraded"] = True
    out = run(world, tmp_path)
    assert out.code == EXIT_OK
    assert "The API reported itself DEGRADED: 1 agent(s) could not be read (delta)" in out.out


# ── refusals and outages ────────────────────────────────────────
@pytest.mark.parametrize("where", ["rpc", "horizon"])
def test_mainnet_is_refused_before_any_claim_is_read(tmp_path: Path, where: str) -> None:
    world = healthy_world()
    setattr(world, f"{where}_passphrase", MAINNET_PASSPHRASE)
    out = run(world, tmp_path)
    assert out.code == EXIT_REFUSED
    assert f"REFUSED: {'the RPC' if where == 'rpc' else 'Horizon'}'s network is '{MAINNET_PASSPHRASE}'" in out.out
    assert not any(c.startswith("api ") for c in world.calls)
    assert not (tmp_path / "evidence").exists()


def test_api_reporting_mainnet_is_refused(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["network"] = "mainnet"
    out = run(world, tmp_path)
    assert out.code == EXIT_REFUSED
    assert "the API reports network 'mainnet'" in out.out
    assert not any(c == "rpc getTransaction" for c in world.calls)


def test_bad_contract_flag_is_refused(tmp_path: Path) -> None:
    stream = io.StringIO()
    code = main(["--api", API, "--escrow", OP1, "--registry", REGISTRY], stream=stream)
    assert code == EXIT_REFUSED
    assert "--escrow must be a contract id" in stream.getvalue()


def test_missing_endpoint_and_bad_shape_are_api_errors(tmp_path: Path) -> None:
    world = healthy_world()
    world.api_status = 404
    out = run(world, tmp_path)
    assert out.code == EXIT_API_UNREADABLE
    assert "answered HTTP 404" in out.out
    world = healthy_world()
    world.payload["operators"][0]["agents"][0]["bound"] = "yes"
    out = run(world, tmp_path)
    assert out.code == EXIT_API_UNREADABLE
    assert "operators[0].agents[0].bound: expected bool, got str" in out.out


def test_rpc_outage_is_unverified_not_forged(tmp_path: Path) -> None:
    world = healthy_world()
    answer = world.rpc

    def down_for_transactions(payload: dict[str, Any]) -> httpx.Response:
        if payload.get("method") == "getTransaction":
            return httpx.Response(503, json={"error": "down"})
        return answer(payload)

    world.rpc = down_for_transactions  # type: ignore[method-assign]
    out = run(world, tmp_path)
    assert out.code == EXIT_CHAIN_UNREADABLE
    assert "exit 8: UNVERIFIED" in out.out
    tx_fails = [f for f in out.fails() if "tx_success" in f]
    assert len(tx_fails) == 3 and all("could not read tx" in f for f in tx_fails)
    assert sum(1 for c in world.calls if c == "rpc getTransaction") == 3 * 4  # bounded: 4 attempts per tx
