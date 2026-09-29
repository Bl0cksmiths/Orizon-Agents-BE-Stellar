"""The demo pre-flight end to end, against an in-memory deployment (story 5.04).

Every test drives `scripts.demo_preflight.cli.main` exactly as an operator
would. `healthy_world()` is a deployment ready to record; each test breaks one
fact and asserts the check that guards it FAILS (or is SKIPPED, or WARNs),
names the fix, and that the exit code keeps the verdict at NO-GO.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts.demo_preflight.checks import FAIL, PASS, SKIPPED, WARN
from scripts.demo_preflight.cli import main
from scripts.demo_preflight.config import EXIT_GO, EXIT_INCOMPLETE, EXIT_NO_GO, EXIT_REFUSED, FRONTEND_PAGES
from scripts.demo_preflight.fakes import (
    API,
    BACKEND,
    BUYER,
    ESCROW,
    FRONTEND,
    HORIZON,
    MAINNET_PASSPHRASE,
    OP1,
    OTHER_SIGNER,
    RPC,
    SIGNER,
    TEAM,
    TEAM_BUYER,
    FakeWorld,
    agent_row,
    healthy_world,
    ready_steps,
    rep,
)

INTENT = "summarise the quarterly numbers"


@dataclass
class Outcome:
    code: int
    out: str
    dir: Path
    world: FakeWorld

    def report(self) -> dict[str, Any]:
        return json.loads((self.dir / "demo-preflight.json").read_text())

    def check(self, check_id: str) -> dict[str, Any]:
        for c in self.report()["checks"]:
            if c["id"] == check_id:
                return c
        raise AssertionError(f"no check {check_id}: {[c['id'] for c in self.report()['checks']]}")

    def status(self, check_id: str) -> str:
        return str(self.check(check_id)["status"])


def run(
    world: FakeWorld,
    tmp_path: Path,
    *extra: str,
    buyer: str | None = BUYER,
    operator: str | None = OP1,
    register: Path | None = None,
) -> Outcome:
    stream = io.StringIO()
    out_dir = tmp_path / "preflight"
    reg = register or FakeWorld.write_register(tmp_path / "team_wallets.json")
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    wallets: list[str] = []
    if buyer is not None:
        wallets += ["--buyer", buyer]
    if operator is not None:
        wallets += ["--operator", operator]
    code = main(
        [
            "--api",
            API + "/api",
            "--backend",
            BACKEND,
            "--frontend",
            FRONTEND,
            "--rpc-url",
            RPC,
            "--horizon-url",
            HORIZON,
            "--team-register",
            str(reg),
            "--cap",
            "2",
            "--out-dir",
            str(out_dir),
            *wallets,
            *extra,
        ],
        transport=world.transport(),
        stream=stream,
        sleep=sleep,
        clock=lambda: clock[0],
        now=lambda: 1_790_000_000.0,
    )
    return Outcome(code, stream.getvalue(), out_dir, world)


# ── the ready deployment ────────────────────────────────────────
def test_ready_deployment_is_go(tmp_path: Path) -> None:
    out = run(healthy_world(), tmp_path, "--with-decompose", INTENT)
    assert out.code == EXIT_GO, out.out
    report = out.report()
    assert report["verdict"] == "GO"
    assert all(c["status"] in (PASS, WARN) for c in report["checks"] if c["required"]), out.out
    assert out.status("exclusion.decompose") == PASS
    assert "exit 0: GO" in out.out
    assert (out.dir / "demo-preflight.md").read_text().startswith("# Demo pre-flight (story 5.04)")


def test_every_check_is_in_the_report(tmp_path: Path) -> None:
    ids = [c["id"] for c in run(healthy_world(), tmp_path).report()["checks"]]
    for expected in (
        "network.warm",
        "network.api",
        "build.adoption",
        "build.agent_readiness",
        "build.readiness",
        "escrow.version",
        "escrow.settler",
        "refunds.enabled",
        "refunds.store",
        "refunds.settler_balance",
        "operator.external",
        "operator.ready",
        "exclusion.below_floor",
        "exclusion.decompose",
        "wallets.buyer",
        "wallets.operator",
        "wallets.team",
        *(f"frontend.{p}" for p in FRONTEND_PAGES),
    ):
        assert expected in ids


def test_only_reads_are_made_without_decompose(tmp_path: Path) -> None:
    world = healthy_world()
    run(world, tmp_path)
    assert not [c for c in world.calls if " POST " in f" {c} " and not c.startswith("rpc")], world.calls
    methods = {c.split(" ")[1] for c in world.calls if c.startswith("rpc ")}
    assert methods == {"getNetwork", "simulateTransaction"}


def test_decompose_is_one_post_never_retried(tmp_path: Path) -> None:
    world = healthy_world()
    world.decompose_status = 503
    out = run(world, tmp_path, "--with-decompose", INTENT)
    assert [c for c in world.calls if c == "api POST /api/orchestrator/decompose"] == [
        "api POST /api/orchestrator/decompose"
    ]
    assert out.status("exclusion.decompose") == FAIL
    assert out.code == EXIT_NO_GO


def test_every_finding_that_is_not_a_pass_names_a_fix(tmp_path: Path) -> None:
    world = healthy_world()
    world.escrow_version = 1
    world.adoption = None
    world.pages["/app/bind"] = 404
    out = run(world, tmp_path, buyer=None)
    for c in out.report()["checks"]:
        if c["status"] != PASS:
            assert c["fix"], c
        else:
            assert c["fix"] == "", c


# ── the network ─────────────────────────────────────────────────
@pytest.mark.parametrize("where", ["rpc", "horizon", "api"])
def test_mainnet_is_refused(tmp_path: Path, where: str) -> None:
    world = healthy_world()
    setattr(world, f"{where}_passphrase", MAINNET_PASSPHRASE)
    out = run(world, tmp_path)
    assert out.code == EXIT_REFUSED
    assert "REFUSED:" in out.out and "testnet only" in out.out
    assert not (out.dir / "demo-preflight.json").exists()
    assert not [c for c in world.calls if c.startswith("frontend")]


def test_adoption_on_another_network_is_refused(tmp_path: Path) -> None:
    world = healthy_world()
    assert world.adoption is not None
    world.adoption["network"] = "mainnet"
    assert run(world, tmp_path).code == EXIT_REFUSED


def test_unconfirmable_network_is_refused(tmp_path: Path) -> None:
    world = healthy_world()
    world.rpc_down = True
    out = run(world, tmp_path)
    assert out.code == EXIT_REFUSED
    assert "could not confirm the network" in out.out


def test_cold_start_is_waited_for_and_reported(tmp_path: Path) -> None:
    world = healthy_world()
    world.health_failures = 3
    out = run(world, tmp_path)
    assert out.code == EXIT_GO, out.out
    assert out.status("network.warm") == PASS
    assert "cold start" in out.check("network.warm")["detail"]
    assert out.report()["cold_start_seconds"] == pytest.approx(14.0)  # 2 + 4 + 8 s of backoff


def test_a_backend_that_never_wakes_fails_and_skips_what_needs_it(tmp_path: Path) -> None:
    world = healthy_world()
    world.health_down = True
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    warm = out.check("network.warm")
    assert warm["status"] == FAIL and "within 120 s" in warm["detail"] and "Render" in warm["fix"]
    for dependent in ("network.api", "build.adoption", "exclusion.below_floor"):
        assert out.status(dependent) == SKIPPED
    # Reads that do not need the API still ran.
    assert out.status("frontend./app/register") == PASS
    assert out.status("wallets.buyer") == PASS


# ── the deployed build ──────────────────────────────────────────
def test_a_build_without_the_adoption_route_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.adoption = None
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("build.adoption")
    assert check["status"] == FAIL and "predates story 5.02" in check["detail"]
    assert out.status("operator.external") == SKIPPED


def test_a_build_without_the_readiness_route_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.readiness_route = False
    out = run(world, tmp_path)
    check = out.check("build.agent_readiness")
    assert check["status"] == FAIL and "/api/agents/alpha/readiness answered 404" in check["detail"]
    assert out.code == EXIT_NO_GO


@pytest.mark.parametrize("field", ["disputes", "escrow"])
def test_a_build_without_the_5_01_readiness_fields_fails(tmp_path: Path, field: str) -> None:
    world = healthy_world()
    assert world.readiness is not None
    del world.readiness[field]
    out = run(world, tmp_path)
    check = out.check("build.readiness")
    assert check["status"] == FAIL and "predates story 5.01" in check["detail"] and field in check["detail"]
    assert out.code == EXIT_NO_GO


def test_a_backend_host_that_is_another_deployment_fails(tmp_path: Path) -> None:
    world = healthy_world()
    assert world.readiness is not None
    world.readiness["escrow"]["contract"] = "CSOMETHINGELSE"
    out = run(world, tmp_path)
    check = out.check("build.readiness")
    assert check["status"] == FAIL and "not the same deployment" in check["detail"]
    assert "--backend" in check["fix"]


def test_a_backend_that_is_not_ready_fails(tmp_path: Path) -> None:
    world = healthy_world()
    assert world.readiness is not None
    world.readiness.update(status="not_ready", llm="missing_key")
    out = run(world, tmp_path)
    check = out.check("build.readiness")
    assert check["status"] == FAIL and "OPENAI_API_KEY" in check["fix"]


def test_readiness_unreachable_on_the_backend_host_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.readiness = None
    out = run(world, tmp_path)
    check = out.check("build.readiness")
    assert check["status"] == FAIL and "--backend" in check["fix"]
    assert out.status("refunds.enabled") == SKIPPED


# ── the escrow ──────────────────────────────────────────────────
def test_a_v1_escrow_fails_naming_d039(tmp_path: Path) -> None:
    world = healthy_world()
    world.escrow_version = 1
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("escrow.version")
    assert check["status"] == FAIL
    assert "D-039" in check["detail"] and ESCROW in check["detail"]
    assert "STELLAR_PAYMENT_ESCROW" in check["fix"]
    assert out.status("escrow.settler") == SKIPPED


def test_an_rpc_outage_is_never_read_as_v1(tmp_path: Path) -> None:
    world = healthy_world()
    world.simulate_down = True
    out = run(world, tmp_path)
    check = out.check("escrow.version")
    assert check["status"] == FAIL
    assert "could not be read" in check["detail"] and "D-039" not in check["detail"]


def test_a_settler_that_is_not_the_signing_key_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.settler = OTHER_SIGNER
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("escrow.settler")
    assert check["status"] == FAIL
    assert OTHER_SIGNER in check["detail"] and SIGNER in check["detail"] and "Unauthorized" in check["detail"]


def test_no_signing_key_fails_the_settler_check(tmp_path: Path) -> None:
    world = healthy_world()
    assert world.readiness is not None
    world.readiness["ratings"]["signer"] = None
    out = run(world, tmp_path)
    check = out.check("escrow.settler")
    assert check["status"] == FAIL and "STELLAR_SIGNING_KEY" in check["fix"]
    assert "no usable signing key" in check["detail"]


# ── refunds ─────────────────────────────────────────────────────
def test_refunds_off_fails(tmp_path: Path) -> None:
    world = healthy_world()
    assert world.readiness is not None
    world.readiness["disputes"]["reconcile"]["enabled"] = False
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("refunds.enabled")
    assert check["status"] == FAIL
    assert "DISPUTE_REFUNDS_ENABLED=true" in check["fix"] and "REFUND_RECONCILE_ENABLED=true" in check["fix"]


def test_a_durable_dispute_store_passes(tmp_path: Path) -> None:
    check = run(healthy_world(), tmp_path).check("refunds.store")
    assert check["status"] == PASS and check["required"] is True


def test_an_in_memory_dispute_store_fails_and_gates(tmp_path: Path) -> None:
    world = healthy_world()
    assert world.readiness is not None
    world.readiness["disputes"]["store"] = "memory"
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO, out.out
    check = out.check("refunds.store")
    assert check["status"] == FAIL and check["required"] is True
    assert "'memory'" in check["detail"] and "DATABASE_URL" in check["fix"]


def test_a_settler_that_cannot_pay_a_refund_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.fund(SIGNER, 3.5)  # 2.5 spendable above the 1 XLM reserve; 1.0 refund + 2.0 fees needs 3.0
    out = run(world, tmp_path, "--max-refund", "1.0")
    check = out.check("refunds.settler_balance")
    assert check["status"] == FAIL
    assert "2.5000000" in check["detail"] and "3.0000000" in check["detail"]
    assert "0.5000000" in check["fix"]
    assert out.code == EXIT_NO_GO


def test_the_settler_reserve_counts_its_subentries(tmp_path: Path) -> None:
    world = healthy_world()
    world.fund(SIGNER, 4.5, subentries=2)  # reserve 2.0 → 2.5 spendable
    assert run(world, tmp_path).status("refunds.settler_balance") == FAIL
    world.fund(SIGNER, 4.5)  # reserve 1.0 → 3.5 spendable
    assert run(world, tmp_path).status("refunds.settler_balance") == PASS


# ── the external operator ───────────────────────────────────────
def test_no_external_operator_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.adoption = world.adoption_payload({})
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("operator.external")
    assert check["status"] == FAIL and "onboarding-session-runbook" in check["fix"]
    assert out.status("operator.ready") == SKIPPED


def test_an_external_operator_that_is_not_ready_is_named_with_its_step(tmp_path: Path) -> None:
    world = healthy_world()
    world.agent_readiness["beta"] = {
        "agent_id": "beta",
        "checked_at": 1,
        "ready": False,
        "steps": ready_steps(reachable="failed"),
    }
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("operator.ready")
    assert check["status"] == FAIL
    assert "beta: `reachable` is failed" in check["detail"] and "fix reachable" in check["detail"]
    assert "alpha" not in check["detail"]


def test_ready_true_without_reachable_done_is_not_ready(tmp_path: Path) -> None:
    world = healthy_world()
    world.agent_readiness["alpha"]["steps"] = ready_steps(reachable="unknown")
    out = run(world, tmp_path)
    assert out.status("operator.ready") == FAIL
    assert "alpha: `reachable` is unknown" in out.check("operator.ready")["detail"]


def test_the_exclusion_subject_is_not_held_to_routable(tmp_path: Path) -> None:
    out = run(healthy_world(), tmp_path)
    check = out.check("operator.ready")
    assert check["status"] == PASS
    assert "below the floor by design" in check["detail"] and "lowrep" in check["detail"]


def test_an_external_operator_whose_only_agent_is_below_the_floor_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.adoption = world.adoption_payload({OP1: ["lowrep"]})
    out = run(world, tmp_path)
    check = out.check("operator.ready")
    assert check["status"] == FAIL and "none can serve the recording" in check["detail"]


# ── the exclusion moment ────────────────────────────────────────
def test_no_below_floor_agent_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.reputations["lowrep"] = rep("lowrep", 5600)
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("exclusion.below_floor")
    assert check["status"] == FAIL and "below 5500 bps" in check["detail"]


@pytest.mark.parametrize("flag", ["degraded", "stale"])
def test_a_degraded_or_stale_below_floor_read_never_counts(tmp_path: Path, flag: str) -> None:
    world = healthy_world()
    world.reputations["lowrep"] = rep("lowrep", 4100, **{flag: True})
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("exclusion.below_floor")
    assert check["status"] == FAIL
    assert f"lowrep (4100 bps, {flag})" in check["detail"]


def test_a_seeded_agent_below_the_floor_does_not_count(tmp_path: Path) -> None:
    world = healthy_world()
    world.reputations["lowrep"] = rep("lowrep", 5600)
    world.reputations["agt_01h8"] = rep("agt_01h8", 3000)
    assert run(world, tmp_path).status("exclusion.below_floor") == FAIL


def test_an_unlisted_agent_below_the_floor_does_not_count(tmp_path: Path) -> None:
    world = healthy_world()
    world.agents = [a for a in world.agents if a["id"] != "lowrep"]
    assert run(world, tmp_path).status("exclusion.below_floor") == FAIL


def test_reputation_routing_off_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.params["enabled"] = False
    check = run(world, tmp_path).check("exclusion.below_floor")
    assert check["status"] == FAIL and "REPUTATION_ENABLED" in check["fix"]


def test_a_plan_that_does_not_exclude_the_agent_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.decompose_body["notices"] = [
        {"kind": "excluded", "agent_id": "someone_else", "reason": "no endpoint", "floor_bps": 5500}
    ]
    out = run(world, tmp_path, "--with-decompose", INTENT)
    check = out.check("exclusion.decompose")
    assert check["status"] == FAIL and check["required"] is True and "lowrep" in check["detail"]
    assert out.code == EXIT_NO_GO


def test_a_plan_that_substitutes_the_agent_passes(tmp_path: Path) -> None:
    world = healthy_world()
    world.decompose_body["notices"][0]["kind"] = "substituted"
    assert run(world, tmp_path, "--with-decompose", INTENT).status("exclusion.decompose") == PASS


def test_decompose_is_skipped_and_advisory_by_default(tmp_path: Path) -> None:
    out = run(healthy_world(), tmp_path)
    check = out.check("exclusion.decompose")
    assert check["status"] == SKIPPED and check["required"] is False
    assert out.code == EXIT_GO


# ── wallets ─────────────────────────────────────────────────────
def test_an_unfunded_buyer_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.fund(BUYER, 3.0)  # 2.0 spendable; --cap 2 + 0.5 fees needs 2.5
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("wallets.buyer")
    assert check["status"] == FAIL and "2.5000000" in check["detail"] and BUYER in check["fix"]


def test_a_buyer_that_does_not_exist_fails(tmp_path: Path) -> None:
    world = healthy_world()
    del world.accounts[BUYER]
    check = run(world, tmp_path).check("wallets.buyer")
    assert check["status"] == FAIL and "does not exist" in check["detail"] and "friendbot" in check["fix"]


def test_an_operator_without_a_bound_agent_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.agents = [agent_row("alpha", OP1, bound=False)] + [a for a in world.agents if a["id"] != "alpha"]
    check = run(world, tmp_path).check("wallets.operator")
    assert check["status"] == FAIL and "/app/bind" in check["fix"]


def test_an_unfunded_operator_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.fund(OP1, 1.2)
    assert run(world, tmp_path).status("wallets.operator") == FAIL


def test_a_team_operator_without_the_flag_fails(tmp_path: Path) -> None:
    world = healthy_world()
    world.agents.append(agent_row("team_agent", TEAM))
    out = run(world, tmp_path, operator=TEAM)
    assert out.code == EXIT_NO_GO
    check = out.check("wallets.team")
    assert check["status"] == FAIL and TEAM in check["detail"] and "--allow-team-operator" in check["fix"]
    assert out.report()["disclosures"] == []


def test_a_team_operator_with_the_flag_warns_and_must_be_disclosed(tmp_path: Path) -> None:
    world = healthy_world()
    world.agents.append(agent_row("team_agent", TEAM))
    out = run(world, tmp_path, "--allow-team-operator", operator=TEAM)
    assert out.code == EXIT_GO, out.out
    check = out.check("wallets.team")
    assert check["status"] == WARN and "Disclose on camera" in check["fix"]
    disclosures = out.report()["disclosures"]
    assert len(disclosures) == 1 and TEAM in disclosures[0] and "on camera" in disclosures[0]
    assert f"DISCLOSE: The operator {TEAM}" in out.out
    assert "## Disclose on camera" in (out.dir / "demo-preflight.md").read_text()


def test_a_team_buyer_fails_without_the_flag(tmp_path: Path) -> None:
    register = FakeWorld.write_register(tmp_path / "reg.json", [TEAM, SIGNER, TEAM_BUYER])
    out = run(healthy_world(), tmp_path, buyer=TEAM_BUYER, register=register)
    assert out.status("wallets.team") == FAIL


def test_no_buyer_is_skipped_and_skipped_is_never_a_pass(tmp_path: Path) -> None:
    out = run(healthy_world(), tmp_path, buyer=None)
    assert out.status("wallets.buyer") == SKIPPED
    assert out.code == EXIT_INCOMPLETE
    assert out.report()["verdict"] == "NO-GO"
    assert "skipped is not a pass" in out.out


def test_a_seed_given_as_the_buyer_is_refused_unechoed(tmp_path: Path) -> None:
    seed = "S" + "A" * 55
    out = run(healthy_world(), tmp_path, buyer=seed)
    assert out.code == EXIT_REFUSED
    assert seed not in out.out and "SECRET seed" in out.out


def test_an_unreadable_team_register_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"wallets": []}))
    assert run(healthy_world(), tmp_path, register=bad).code == EXIT_REFUSED


# ── the frontend ────────────────────────────────────────────────
@pytest.mark.parametrize("status", [404, 500, 307])
def test_a_page_that_is_not_200_fails(tmp_path: Path, status: int) -> None:
    world = healthy_world()
    world.pages["/guide/list-your-agent"] = status
    out = run(world, tmp_path)
    assert out.code == EXIT_NO_GO
    check = out.check("frontend./guide/list-your-agent")
    assert check["status"] == FAIL and f"answered {status}" in check["detail"]
    if status == 307:
        assert "/login" in check["detail"]


@pytest.mark.parametrize("path", ["/app/trace", "/demo"])
def test_the_trace_and_demo_pages_are_required(tmp_path: Path, path: str) -> None:
    assert run(healthy_world(), tmp_path).status(f"frontend.{path}") == PASS
    world = healthy_world()
    world.pages[path] = 404
    out = run(world, tmp_path)
    check = out.check(f"frontend.{path}")
    assert check["status"] == FAIL and check["required"] is True and "answered 404" in check["detail"]
    assert out.code == EXIT_NO_GO
