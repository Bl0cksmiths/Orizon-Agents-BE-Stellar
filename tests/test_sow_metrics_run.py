"""The SOW §6.3 metrics generator end to end, against an in-memory testnet and deployment (story 5.05).

Every test drives `scripts.sow_metrics.cli.main` exactly as an operator would.
`met_world()` is a sprint in which all eleven metrics are met; each test
breaks one fact and asserts the metric that guards it misses and says why in
plain words — or, when a read fails, that the metric is "Not measured": never
a 0, never met, and the exit code says so.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts.sow_metrics.cli import main
from scripts.sow_metrics.config import (
    BLOCK_NAME,
    DEMO_PAGE,
    EXIT_MEASURED,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    GUIDE_PAGE,
    MARKDOWN_NAME,
    RAW_NAME,
    REGISTER_PAGE,
    REGISTER_ROUTE,
)
from scripts.sow_metrics.fakes import (
    A1,
    ADMIN,
    API,
    BACKEND,
    BUYER1,
    BUYER2,
    DISPATCH,
    ESCROW_V1,
    ESCROW_V2,
    FRONTEND,
    GITHUB,
    HORIZON,
    JOB1,
    LEDGER,
    MAINNET_PASSPHRASE,
    OP1,
    OP2,
    REGISTER_ROLES,
    REGISTRY,
    RPC,
    SIGNER,
    TEAM_BUYER,
    TEAM_OP,
    FakeWorld,
    demo_html,
    job,
    met_world,
)

ALL = [f"m{i:02d}" for i in range(1, 12)]


@dataclass
class Outcome:
    code: int
    out: str
    dir: Path
    world: FakeWorld

    def block(self) -> list[dict[str, Any]]:
        return json.loads((self.dir / BLOCK_NAME).read_text())

    def raw(self) -> dict[str, Any]:
        return json.loads((self.dir / RAW_NAME).read_text())

    def metric(self, metric_id: str) -> dict[str, Any]:
        return next(m for m in self.block() if m["id"] == metric_id)

    def raw_metric(self, metric_id: str) -> dict[str, Any]:
        return next(m for m in self.raw()["metrics"] if m["id"] == metric_id)

    def labels(self, metric_id: str) -> list[str]:
        return [link["label"] for link in self.metric(metric_id)["links"]]

    def status(self, metric_id: str) -> str:
        return str(self.metric(metric_id)["status"])


def run(world: FakeWorld, tmp_path: Path, *extra: str, register: Path | None = None) -> Outcome:
    stream = io.StringIO()
    out_dir = tmp_path / "metrics"
    reg = register or FakeWorld.write_register(tmp_path / "team_wallets.json")
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
            "--github-api",
            GITHUB,
            "--team-register",
            str(reg),
            "--out-dir",
            str(out_dir),
            *extra,
        ],
        transport=world.transport(),
        stream=stream,
        sleep=lambda _s: None,
        now=lambda: 1_790_000_000.0,
    )
    return Outcome(code, stream.getvalue(), out_dir, world)


def assert_unmeasured(out: Outcome, metric_id: str) -> None:
    m = out.metric(metric_id)
    assert m["status"] == "not_met", m
    assert m["achieved"] == "Not measured", m
    assert not any(ch.isdigit() for ch in m["achieved"])
    assert m["reason"].startswith("Not measured:"), m["reason"]
    assert out.raw_metric(metric_id)["measured"] is False


# ── the met sprint ──────────────────────────────────────────────
def test_a_sprint_that_meets_every_target_is_measured_as_met(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    assert out.code == EXIT_MEASURED, out.out
    assert [m["status"] for m in out.block()] == ["met"] * 11
    achieved = {m["id"]: m["achieved"] for m in out.block()}
    assert achieved == {
        "m01": "3",
        "m02": "2",
        "m03": "3",
        "m04": "4",
        "m05": "1",
        **{i: "Yes" for i in ALL[5:]},
    }
    assert all("reason" not in m for m in out.block())
    assert "exit 0: 11 of 11 met" in out.out
    for name in (BLOCK_NAME, MARKDOWN_NAME, RAW_NAME):
        assert (out.dir / name).is_file()


def test_a_measured_miss_still_exits_zero(tmp_path: Path) -> None:
    world = met_world()
    world.pages[GUIDE_PAGE] = (404, "<html>nope</html>")
    out = run(world, tmp_path)
    assert out.code == EXIT_MEASURED
    assert out.status("m09") == "not_met"
    assert "exit 0: 10 of 11 met" in out.out


def test_the_generator_only_reads(tmp_path: Path) -> None:
    """FakeWorld refuses any non-GET outside the RPC; the RPC is asked only reads."""
    world = met_world()
    run(world, tmp_path)
    rpc = {c for c in world.calls if c.startswith("rpc ")}
    assert rpc <= {"rpc getNetwork", "rpc simulateTransaction", "rpc getLedgerEntries"}


# ── m01 / m02: external agents and wallets ──────────────────────
def test_team_owned_agents_are_excluded_with_the_register_role(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    labels = out.labels("m01")
    assert any(
        label.startswith("Registration of qa_agent by the team's QA throwaway operator key — 2026-09-17")
        and label.endswith("(excluded: team wallet)")
        for label in labels
    ), labels
    excluded = {e["agent_id"]: e["reason"] for e in out.raw_metric("m01")["excluded"]}
    assert excluded["qa_agent"] == "team wallet"
    assert excluded["house_agent"] == "team wallet"


def test_an_agent_owned_by_a_platform_key_not_in_the_register_is_excluded(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    excluded = {e["agent_id"]: e["reason"] for e in out.raw_metric("m01")["excluded"]}
    assert excluded["signer_agent"] == "platform key"
    assert any("signer_agent by the platform's ratings signer" in label for label in out.labels("m01"))


@pytest.mark.parametrize(
    ("role_source", "key"),
    [("network admin", ADMIN), ("dispatch signer", DISPATCH), ("registry admin", "registry"), ("sealer", "sealer")],
)
def test_every_runtime_platform_key_is_excluded(tmp_path: Path, role_source: str, key: str) -> None:
    """An outside-looking owner that turns out to be a runtime key is not external."""
    world = met_world()
    stranger = OP1
    if key == "registry":
        world.registry_admin = stranger
    elif key == "sealer":
        world.attest_roles["Sealer"] = stranger
    else:
        stranger = key
        world.agents["alpha"]["owner"] = key
    register = FakeWorld.write_register(tmp_path / "r.json", {TEAM_OP: "QA key"})  # ADMIN not in the register
    out = run(world, tmp_path, register=register)
    owners = {e["agent_id"]: e["reason"] for e in out.raw_metric("m01")["excluded"]}
    assert owners.get("alpha") == "platform key", (role_source, owners)


def test_one_outside_operator_misses_both_adoption_targets(tmp_path: Path) -> None:
    register = FakeWorld.write_register(tmp_path / "r.json", {**REGISTER_ROLES, OP2: "second QA operator key"})
    out = run(met_world(), tmp_path, register=register)
    assert out.status("m01") == "not_met" and out.metric("m01")["achieved"] == "1"
    assert out.metric("m01")["reason"] == (
        "Only 1 agent is registered by outside operators; the other 5 belong to wallets the team controls. "
        "The target is 2."
    )
    assert out.status("m02") == "not_met" and out.metric("m02")["achieved"] == "1"
    assert out.metric("m02")["reason"] == "Only 1 outside operator wallet owns an agent. The target is 2."


def test_no_outside_operator_is_stated_plainly(tmp_path: Path) -> None:
    register = FakeWorld.write_register(tmp_path / "r.json", {**REGISTER_ROLES, OP1: "k1", OP2: "k2"})
    out = run(met_world(), tmp_path, register=register)
    assert out.metric("m01")["achieved"] == "0"
    assert out.metric("m01")["reason"] == (
        "No outside operator has registered an agent yet; all 6 registered agents belong to 5 wallets the team "
        "controls."
    )
    assert out.metric("m03")["achieved"] == "0"
    assert out.metric("m03")["reason"].startswith("No agent run by an outside operator exists yet")


# ── m03 / m04: settlements ──────────────────────────────────────
def test_a_self_payment_by_the_owner_is_excluded(tmp_path: Path) -> None:
    world = met_world()
    world.settle_v2(OP1, [("alpha", 700_000)], job("self"), at="2026-09-25")
    out = run(world, tmp_path)
    assert out.metric("m04")["achieved"] == "4"
    reasons = [e["reasons"] for e in out.raw_metric("m04")["excluded"] if e["job_id"] == job("self")]
    assert reasons == [["self-payment (the payer owns the agent)"]]
    assert any(
        label.startswith("Charge of 0.07 XLM to alpha on the v2 escrow — 2026-09-25 (excluded: self-payment")
        for label in out.labels("m04")
    )


def test_a_payment_by_the_settler_is_excluded(tmp_path: Path) -> None:
    world = met_world()
    world.settle_v2(SIGNER, [("beta", 700_000)], job("by-settler"), at="2026-09-25")
    out = run(world, tmp_path)
    reasons = [e["reasons"] for e in out.raw_metric("m04")["excluded"] if e["job_id"] == job("by-settler")]
    assert reasons == [["self-payment (the payer is the escrow's settler)"]]
    assert out.metric("m04")["achieved"] == "4"


def test_a_payment_by_a_platform_key_is_excluded(tmp_path: Path) -> None:
    world = met_world()
    world.settle_v2(DISPATCH, [("beta", 700_000)], job("by-dispatch"), at="2026-09-25")
    out = run(world, tmp_path)
    reasons = [e["reasons"] for e in out.raw_metric("m04")["excluded"] if e["job_id"] == job("by-dispatch")]
    assert reasons == [["self-payment (the payer is the platform's dispatch signer)"]]


def test_a_team_buyer_paying_an_outside_agent_counts_as_a_settlement(tmp_path: Path) -> None:
    """The rule is self-payment, not team membership: a team QA buyer paying an outside operator moves money."""
    world = met_world()
    world.settle_v2(TEAM_BUYER, [("beta", 700_000)], job("team-buyer"), at="2026-09-25")
    out = run(world, tmp_path)
    assert out.metric("m04")["achieved"] == "5"
    assert out.metric("m03")["achieved"] == "4"


def test_a_pre_sprint_settlement_is_excluded(tmp_path: Path) -> None:
    world = met_world()
    world.settle_v2(BUYER2, [("beta", 700_000)], job("early"), at="2026-09-06T23:59:59")
    out = run(world, tmp_path)
    reasons = [e["reasons"] for e in out.raw_metric("m04")["excluded"] if e["job_id"] == job("early")]
    assert reasons == [["settled before the sprint began on 2026-09-07"]]
    assert out.metric("m04")["achieved"] == "4"


def test_a_settlement_on_the_first_sprint_day_counts(tmp_path: Path) -> None:
    world = met_world()
    world.settle_v2(BUYER2, [("beta", 700_000)], job("day-one"), at="2026-09-07T00:00:00")
    assert run(world, tmp_path).metric("m04")["achieved"] == "5"


def test_a_charge_to_a_team_agent_counts_for_charges_but_not_for_external_workflows(tmp_path: Path) -> None:
    world = met_world()
    world.settle_v2(BUYER2, [("qa_agent", 700_000)], job("team-agent"), at="2026-09-25")
    out = run(world, tmp_path)
    assert out.metric("m04")["achieved"] == "5"
    assert out.metric("m03")["achieved"] == "3"
    reasons = [e["reasons"] for e in out.raw_metric("m03")["excluded"] if e["job_id"] == job("team-agent")]
    assert reasons == [["the agent is run by the team's QA throwaway operator key"]]


def test_two_payouts_of_one_job_are_two_charges_but_one_workflow(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    workflows = out.raw_metric("m03")["counted"]
    assert len(workflows) == 3
    assert sorted(len(w["charges"]) for w in workflows) == [1, 1, 2]


def test_too_few_charges_misses_with_the_breakdown(tmp_path: Path) -> None:
    world = met_world()
    world.escrows[ESCROW_V2].ids.clear()
    for name in ("settle1", "settle2", "settle3"):
        world.drop(world.marks[name])
    out = run(world, tmp_path)
    assert out.metric("m04")["achieved"] == "0"
    assert out.metric("m04")["reason"] == (
        "None of the 1 charge on record counts: all 1 was the agent's owner paying itself and all 1 was settled "
        "before the sprint began (2026-05-13). No payment from a buyer to a different agent owner has settled "
        "since the sprint began."
    )
    assert out.status("m03") == "not_met"


def test_a_v1_charge_to_an_outside_agent_counts(tmp_path: Path) -> None:
    """Both escrow versions: a v1 `charge` receipt counts like a v2 payout, with its own tx link."""
    world = met_world()
    tx = world.charge_v1(BUYER2, "beta", 300_000, job("v1-real"), at="2026-09-26")
    out = run(world, tmp_path)
    assert out.metric("m04")["achieved"] == "5"
    assert out.metric("m03")["achieved"] == "4"
    links = [link for link in out.metric("m04")["links"] if link.get("tx_hash") == tx]
    assert links and links[0]["label"] == "Charge of 0.03 XLM to beta on the v1 escrow — 2026-09-26 (counted)"


def test_each_escrow_is_read_by_its_version(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    escrows = {e["contract"]: e for e in out.raw()["summary"]["escrows"]}
    assert escrows[ESCROW_V2]["version"] == 2 and escrows[ESCROW_V2]["live"] is True
    assert escrows[ESCROW_V1]["version"] == 1 and escrows[ESCROW_V1]["live"] is False
    assert escrows[ESCROW_V2]["receipts"] == 4 and escrows[ESCROW_V1]["receipts"] == 1
    kinds = [link["label"] for link in out.metric("m04")["links"] if link["kind"] == "contract"]
    assert kinds == [
        "PaymentEscrow v2 contract (live) (every charge it recorded)",
        "PaymentEscrow v1 contract (every charge it recorded)",
    ]


def test_a_live_v1_escrow_alone_is_measured(tmp_path: Path) -> None:
    """Before v2 is deployed the live escrow IS the known v1: it is read once, as v1."""
    world = met_world()
    world.live_escrow = ESCROW_V1
    del world.escrows[ESCROW_V2]
    out = run(world, tmp_path)
    assert out.code == EXIT_MEASURED
    assert [e["contract"] for e in out.raw()["summary"]["escrows"]] == [ESCROW_V1]
    assert out.metric("m04")["achieved"] == "0"


def test_an_extra_escrow_flag_is_counted(tmp_path: Path) -> None:
    world = met_world()
    extra = "CAAQCAIBAEAQCAIBAEAQCAIBAEAQCAIBAEAQCAIBAEAQCAIBAEAQC526"
    world.add_escrow(extra, version=2, settler=SIGNER)
    world.settle_v2(BUYER2, [("gamma", 400_000)], job("extra"), at="2026-09-26", contract=extra)
    out = run(world, tmp_path, "--escrow", extra)
    assert out.metric("m04")["achieved"] == "5"


# ── m05: dispute → refund ───────────────────────────────────────
def test_a_dispute_refund_needs_the_dispute_rating_and_the_refund(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    counted = out.raw_metric("m05")["counted"]
    assert len(counted) == 1
    assert counted[0]["payer"] == BUYER1 and counted[0]["charge"]["job_id"] == JOB1
    labels = out.labels("m05")
    assert "Dispute rating on alpha — 2026-09-24 (counted: refunded)" in labels
    assert "Refund of 0.05 XLM to the payer of the disputed charge — 2026-09-24 (counted)" in labels


def test_the_drill_transfer_to_a_team_key_is_not_a_dispute_refund(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    excluded = [e for e in out.raw_metric("m05")["excluded"] if e["kind"] == "transfer"]
    assert [(e["destination"], e["reason"]) for e in excluded] == [
        (TEAM_OP, "no dispute behind it; paid to a team wallet")
    ]
    assert (
        "Transfer of 0.054 XLM from the team's contract admin and v1 escrow settler to the team's QA throwaway "
        "operator key — 2026-09-12 (excluded: no dispute behind it; paid to a team wallet)"
    ) in out.labels("m05")


def _no_refund_world() -> FakeWorld:
    world = met_world()
    world.drop(world.marks["refund"])
    return world


def _no_dispute_world() -> FakeWorld:
    world = _no_refund_world()
    world.drop(world.marks["dispute"])
    world.disputed.clear()
    return world


def test_a_dispute_without_a_refund_does_not_count(tmp_path: Path) -> None:
    out = run(_no_refund_world(), tmp_path)
    assert out.metric("m05")["achieved"] == "0"
    reasons = [e["reason"] for e in out.raw_metric("m05")["excluded"] if e["kind"] == "dispute rating"]
    assert reasons == ["no refund from the platform to the charge's payer followed it"]
    assert out.metric("m05")["reason"].startswith("No dispute has been refunded yet. 1 dispute rating is on the ledger")


def test_a_refund_without_a_dispute_does_not_count(tmp_path: Path) -> None:
    world = met_world()
    world.drop(world.marks["dispute"])
    world.disputed.clear()
    out = run(world, tmp_path)
    assert out.metric("m05")["achieved"] == "0"
    transfers = [e for e in out.raw_metric("m05")["excluded"] if e["kind"] == "transfer"]
    assert {e["destination"] for e in transfers} == {TEAM_OP, BUYER1}


def test_a_refund_to_someone_other_than_the_payer_does_not_count(tmp_path: Path) -> None:
    world = _no_refund_world()
    world.transfer(SIGNER, BUYER2, A1 // 2, at="2026-09-24T09:05:00")
    assert run(world, tmp_path).metric("m05")["achieved"] == "0"


def test_a_refund_larger_than_the_disputed_charge_does_not_count(tmp_path: Path) -> None:
    world = _no_refund_world()
    world.transfer(SIGNER, BUYER1, A1 + 1, at="2026-09-24T09:05:00")
    assert run(world, tmp_path).metric("m05")["achieved"] == "0"


def test_a_refund_paid_before_the_charge_settled_does_not_count(tmp_path: Path) -> None:
    world = _no_refund_world()
    world.transfer(SIGNER, BUYER1, A1 // 2, at="2026-09-21T09:00:00")
    assert run(world, tmp_path).metric("m05")["achieved"] == "0"


def test_a_dispute_rating_whose_id_traces_to_no_charge_does_not_count(tmp_path: Path) -> None:
    world = _no_dispute_world()
    world.dispute("alpha", job("never-settled"), BUYER1, at="2026-09-24")
    world.transfer(SIGNER, BUYER1, 100_000, at="2026-09-24T10:00:00")
    out = run(world, tmp_path)
    assert out.metric("m05")["achieved"] == "0"
    reasons = [e["reason"] for e in out.raw_metric("m05")["excluded"] if e["kind"] == "dispute rating"]
    assert reasons == ["its job id traces back to no charge on record"]


def test_a_dispute_on_a_later_step_traces_through_its_step_index(tmp_path: Path) -> None:
    world = _no_dispute_world()
    job3 = job("job-3")
    world.dispute("alpha", job3, BUYER1, step=1, at="2026-09-24")
    world.transfer(SIGNER, BUYER1, 400_000, at="2026-09-24T10:00:00")
    out = run(world, tmp_path)
    assert out.metric("m05")["achieved"] == "1"
    assert out.raw_metric("m05")["counted"][0]["charge"]["job_id"] == job3


def test_a_dispute_on_a_self_payment_does_not_count(tmp_path: Path) -> None:
    world = _no_refund_world()
    self_job = job("self-disputed")
    world.settle_v2(OP1, [("alpha", 700_000)], self_job, at="2026-09-25")
    world.dispute("alpha", self_job, OP1, at="2026-09-26")
    world.transfer(SIGNER, OP1, 100_000, at="2026-09-26T10:00:00")
    out = run(world, tmp_path)
    assert out.metric("m05")["achieved"] == "0"
    reasons = {e["reason"] for e in out.raw_metric("m05")["excluded"] if e["kind"] == "dispute rating"}
    assert "the charge it disputes does not count (self-payment (the payer owns the agent))" in reasons


def test_no_dispute_at_all_is_stated_plainly(tmp_path: Path) -> None:
    out = run(_no_dispute_world(), tmp_path)
    assert out.metric("m05")["reason"] == (
        "No dispute has been refunded yet. The reputation ledger holds 3 ratings (3 of kind auto) and no dispute "
        "rating; its lifetime dispute count is 0 across all 6 agents. The only transfer out of a platform key on "
        "record, on 2026-09-12, went to a team key with no dispute behind it."
    )


def test_a_ledger_dispute_missing_from_the_history_is_unmeasured_not_zero(tmp_path: Path) -> None:
    world = _no_refund_world()
    world.disputed["beta"] = 3
    out = run(world, tmp_path)
    assert out.code == EXIT_UNREADABLE
    assert_unmeasured(out, "m05")
    assert_unmeasured(out, "m08")


# ── m06 – m11: the milestones ───────────────────────────────────
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, "The /app/register page answers HTTP 404: it has not been deployed yet."),
        (307, "The /app/register page redirects (to https://front.test/login) instead of answering."),
        (500, "The /app/register page answers HTTP 500."),
    ],
)
def test_the_register_page_must_answer_with_no_login(tmp_path: Path, status: int, expected: str) -> None:
    world = met_world()
    world.pages[REGISTER_PAGE] = (status, "")
    out = run(world, tmp_path)
    assert out.status("m06") == "not_met" and out.metric("m06")["achieved"] == "No"
    assert out.metric("m06")["reason"] == expected


def test_the_register_route_must_be_live(tmp_path: Path) -> None:
    world = met_world()
    world.routes.discard(REGISTER_ROUTE)
    out = run(world, tmp_path)
    assert out.metric("m06")["reason"] == (
        "The live backend does not publish the route that builds a registration transaction."
    )


def test_the_register_milestone_links_a_non_admin_registration(tmp_path: Path) -> None:
    labels = run(met_world(), tmp_path).labels("m06")
    assert "Registration of gamma signed by an outside operator's wallet, not the registry admin — 2026-09-20" in labels


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (
            {"enabled": False, "floor_bps": 5500},
            "The live reputation settings say reputation-gated routing is switched off.",
        ),
        (
            {"enabled": True, "floor_bps": 0},
            "The live reputation settings name no floor, so no agent can be left out for low reputation.",
        ),
        (
            {"enabled": True},
            "The live reputation settings name no floor, so no agent can be left out for low reputation.",
        ),
        (
            {"enabled": "true", "floor_bps": 5500},
            "The live reputation settings say reputation-gated routing is switched off.",
        ),
    ],
)
def test_reputation_gating_needs_the_floor_on(tmp_path: Path, params: dict[str, Any], expected: str) -> None:
    world = met_world()
    world.params = params
    out = run(world, tmp_path)
    assert out.status("m07") == "not_met" and out.metric("m07")["reason"] == expected


def test_the_dispute_milestone_needs_refunds_switched_on(tmp_path: Path) -> None:
    world = met_world()
    world.readiness["disputes"]["reconcile"]["enabled"] = False
    out = run(world, tmp_path)
    assert out.status("m08") == "not_met"
    assert out.metric("m08")["reason"] == (
        "The dispute window is live, but the live backend does not report refunds as switched on."
    )


def test_a_readiness_without_the_refund_switch_is_not_refunds_on(tmp_path: Path) -> None:
    world = met_world()
    del world.readiness["disputes"]
    out = run(world, tmp_path)
    assert out.metric("m08")["reason"] == (
        "The dispute window is live, but the live backend does not report refunds as switched on (its readiness "
        "report predates the refund switch)."
    )


def test_the_dispute_milestone_needs_every_dispute_route(tmp_path: Path) -> None:
    world = met_world()
    world.routes.discard("POST /api/disputes/{dispute_id}/uphold")
    out = run(world, tmp_path)
    assert out.metric("m08")["reason"] == "The live backend does not publish every dispute route."


def test_the_dispute_milestone_needs_a_real_refund(tmp_path: Path) -> None:
    out = run(_no_refund_world(), tmp_path)
    assert out.status("m08") == "not_met"
    assert out.metric("m08")["reason"] == (
        "The dispute window is live, but no dispute has been refunded on-chain yet (see the dispute refund row)."
    )


def test_the_guide_must_answer_with_no_login(tmp_path: Path) -> None:
    world = met_world()
    world.pages[GUIDE_PAGE] = (307, "")
    out = run(world, tmp_path)
    assert out.metric("m09")["reason"] == (
        "The /guide/list-your-agent page redirects (to https://front.test/login) instead of answering."
    )


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ((404, ""), "The /demo page answers HTTP 404: it has not been deployed yet."),
        ((200, demo_html("unpublished")), "The /demo page is live but says the video has not been published yet."),
        (
            (200, demo_html(None, 222)),
            "The /demo page answers but carries no published marker (data-demo), so the video cannot be confirmed "
            "as published.",
        ),
        ((200, demo_html("published")), "The /demo page says published but shows no running time for the video."),
        (
            (200, demo_html("published", 63)),
            "The published video runs 63 seconds, outside the 3 to 5 minutes the SOW asks for.",
        ),
        (
            (200, demo_html("published", 301)),
            "The published video runs 301 seconds, outside the 3 to 5 minutes the SOW asks for.",
        ),
    ],
)
def test_the_demo_video_must_be_published_and_three_to_five_minutes(
    tmp_path: Path, page: tuple[int, str], expected: str
) -> None:
    world = met_world()
    world.pages[DEMO_PAGE] = page
    out = run(world, tmp_path)
    assert out.status("m10") == "not_met" and out.metric("m10")["reason"] == expected


@pytest.mark.parametrize("seconds", [180, 300])
def test_the_demo_bounds_are_inclusive(tmp_path: Path, seconds: int) -> None:
    world = met_world()
    world.pages[DEMO_PAGE] = (200, demo_html("published", seconds))
    assert run(world, tmp_path).status("m10") == "met"


def test_every_repository_must_carry_mit(tmp_path: Path) -> None:
    world = met_world()
    world.repos["Bl0cksmiths/Orizon-Agents-BE-Stellar"] = (200, None)
    world.repos["Bl0cksmiths/Orizon-Agents-Smart-Contract-Stellar"] = (200, "Apache-2.0")
    world.repos["Bl0cksmiths/Orizon-Agents-Example-Agent-Stellar"] = (404, None)
    out = run(world, tmp_path)
    assert out.metric("m11")["reason"] == (
        "3 of the 4 repositories have no MIT licence that GitHub recognises: the backend (no licence detected), "
        "the smart contracts (licence Apache-2.0) and the example agent (not found)."
    )
    assert out.labels("m11") == [
        "Frontend repository — MIT licence detected",
        "Backend repository — GitHub detects no licence",
        "Smart contracts repository — licence detected: Apache-2.0, not MIT",
        "Example agent repository — not found on GitHub",
    ]


# ── read failures: unmeasured, never 0 ──────────────────────────
def test_simulations_down_leave_the_chain_metrics_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.simulate_down = True
    out = run(world, tmp_path)
    assert out.code == EXIT_UNREADABLE, out.out
    for metric_id in ("m01", "m02", "m03", "m04", "m05", "m08"):
        assert_unmeasured(out, metric_id)
    for metric_id in ("m06", "m07", "m09", "m10", "m11"):
        assert out.status(metric_id) == "met"
    assert "exit 4: 5 of 11 met, 6 not measured" in out.out


def test_the_readiness_unreachable_leaves_the_external_counts_unmeasured(tmp_path: Path) -> None:
    """Without /readiness the ratings signer is unknown, and any owner could be it."""
    world = met_world()
    world.api_down.add("/readiness")
    out = run(world, tmp_path)
    assert out.code == EXIT_UNREADABLE
    for metric_id in ("m01", "m02", "m03", "m04", "m05", "m08"):
        assert_unmeasured(out, metric_id)
    assert "the backend's readiness report" in out.metric("m01")["reason"]


def test_one_unreadable_escrow_id_leaves_the_settlements_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.escrows[ESCROW_V2].ids.append(("gone", {}))
    out = run(world, tmp_path)
    assert out.code == EXIT_UNREADABLE
    for metric_id in ("m03", "m04", "m05"):
        assert_unmeasured(out, metric_id)
    assert out.status("m01") == "met"


def test_the_escrow_state_unreadable_leaves_the_settlements_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.simulate_down_for.add((ESCROW_V2, "receipt"))
    out = run(world, tmp_path)
    for metric_id in ("m03", "m04", "m05", "m08"):
        assert_unmeasured(out, metric_id)


def test_a_platform_history_unreadable_leaves_the_refunds_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.horizon_down_for.add(SIGNER)
    out = run(world, tmp_path)
    assert out.code == EXIT_UNREADABLE
    assert_unmeasured(out, "m05")
    assert_unmeasured(out, "m08")
    assert out.metric("m04")["achieved"] == "4"  # counts come from state; only links need history


def test_an_owner_history_unreadable_only_loses_links(tmp_path: Path) -> None:
    world = met_world()
    world.horizon_down_for.add(OP2)
    out = run(world, tmp_path)
    assert out.code == EXIT_MEASURED
    assert out.metric("m01")["achieved"] == "3"
    assert not any("beta" in label for label in out.labels("m01"))
    assert any("registration history" in w for w in out.raw()["warnings"])


def test_the_ledger_unreadable_leaves_the_refunds_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.simulate_down_for.add((LEDGER, "rep_state"))
    out = run(world, tmp_path)
    assert_unmeasured(out, "m05")


def test_the_instance_storage_unreadable_leaves_the_counts_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.ledger_entries_down = True
    out = run(world, tmp_path)
    for metric_id in ("m01", "m02", "m03", "m04", "m05"):
        assert_unmeasured(out, metric_id)


def test_the_registry_unreadable_leaves_the_adoption_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.simulate_down_for.add((REGISTRY, "get"))
    out = run(world, tmp_path)
    assert_unmeasured(out, "m01")
    assert_unmeasured(out, "m02")


def test_the_reputation_settings_unreachable_are_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.api_down.add("/api/stellar/reputation/params")
    out = run(world, tmp_path)
    assert out.code == EXIT_UNREADABLE
    assert_unmeasured(out, "m07")
    assert out.metric("m07")["reason"].startswith("Not measured: the live reputation settings could not be read")


def test_the_route_list_unreachable_leaves_two_milestones_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.api_down.add("/openapi.json")
    out = run(world, tmp_path)
    assert_unmeasured(out, "m06")
    assert_unmeasured(out, "m08")


def test_github_rate_limited_is_unmeasured_not_unlicensed(tmp_path: Path) -> None:
    world = met_world()
    world.github_status = 403
    out = run(world, tmp_path)
    assert out.code == EXIT_UNREADABLE
    assert_unmeasured(out, "m11")


def test_a_page_that_never_answers_is_unmeasured(tmp_path: Path) -> None:
    world = met_world()
    world.pages[DEMO_PAGE] = (503, "")
    out = run(world, tmp_path)
    assert_unmeasured(out, "m10")
    assert out.metric("m10")["reason"].startswith("Not measured: the /demo page could not be read")


# ── refusals ────────────────────────────────────────────────────
@pytest.mark.parametrize("where", ["rpc", "horizon", "api"])
def test_mainnet_is_refused_before_anything_is_measured(tmp_path: Path, where: str) -> None:
    world = met_world()
    setattr(world, f"{where}_passphrase", MAINNET_PASSPHRASE)
    out = run(world, tmp_path)
    assert out.code == EXIT_REFUSED, out.out
    assert "REFUSED: this is not testnet" in out.out
    assert not (out.dir / BLOCK_NAME).exists()
    assert "rpc simulateTransaction" not in world.calls


def test_an_api_naming_another_network_is_refused(tmp_path: Path) -> None:
    world = met_world()
    world.api_network = "public"
    out = run(world, tmp_path)
    assert out.code == EXIT_REFUSED
    assert "not testnet" in out.out


def test_an_unconfirmable_network_is_a_read_failure(tmp_path: Path) -> None:
    world = met_world()
    world.rpc_down = True
    out = run(world, tmp_path)
    assert out.code == EXIT_UNREADABLE
    assert "UNREADABLE: the network could not be confirmed" in out.out
    assert not (out.dir / BLOCK_NAME).exists()


def test_a_bad_register_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"wallets": []}')
    out = run(met_world(), tmp_path, register=bad)
    assert out.code == EXIT_REFUSED
    assert "names no Stellar account" in out.out


def test_a_bad_escrow_flag_is_refused(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path, "--escrow", BUYER2)
    assert out.code == EXIT_REFUSED
    assert "--escrow must be a contract id" in out.out
