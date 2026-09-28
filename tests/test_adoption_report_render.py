"""The adoption report's output: the Markdown evidence table and the JSON for the 5.05 index."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from scripts.adoption_report.cli import main
from scripts.adoption_report.config import EXIT_OK, EXPLORER
from scripts.adoption_report.fakes import (
    API,
    ESCROW,
    HORIZON,
    OP1,
    OP2,
    PLATFORM,
    REGISTRY,
    RPC,
    FakeWorld,
    healthy_world,
)
from scripts.adoption_report.report import JSON_NAME, MARKDOWN_NAME, SCHEMA


def run(world: FakeWorld, tmp_path: Path) -> tuple[int, str, Path]:
    stream = io.StringIO()
    out_dir = tmp_path / "evidence"
    code = main(
        [
            "--api",
            API + "/api/",
            "--rpc-url",
            RPC,
            "--horizon-url",
            HORIZON,
            "--escrow",
            ESCROW,
            "--registry",
            REGISTRY,
            "--team-register",
            str(FakeWorld.write_register(tmp_path / "team.json")),
            "--out-dir",
            str(out_dir),
        ],
        transport=world.transport(),
        stream=stream,
        sleep=lambda _s: None,
        now=lambda: 1_790_000_200.0,
    )
    return code, stream.getvalue(), out_dir


def test_markdown_links_every_account_transaction_and_contract_to_testnet_stellar_expert(tmp_path: Path) -> None:
    world = healthy_world()
    code, out, out_dir = run(world, tmp_path)
    assert code == EXIT_OK
    markdown = (out_dir / MARKDOWN_NAME).read_text()
    assert markdown.rstrip("\n") in out  # stdout carries the same report
    for owner in (OP1, OP2, PLATFORM):
        assert f"({EXPLORER}/account/{owner})" in markdown
    for tx_hash in world.txs:
        assert f"({EXPLORER}/tx/{tx_hash})" in markdown
    assert f"({EXPLORER}/contract/{ESCROW})" in markdown
    assert f"({EXPLORER}/contract/{REGISTRY})" in markdown
    assert "stellar.expert/explorer/public" not in markdown
    assert "Generated 2026-09-21T" in markdown  # from the injected clock
    assert "| OP-1 |" in markdown and "| OP-2 |" in markdown
    assert "| `alpha` |" in markdown and "| charged event | yes |" in markdown


def test_untrusted_names_cannot_break_the_table(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["operators"][0]["agents"][0]["name"] = "evil | name\nnext"
    _, _, out_dir = run(world, tmp_path)
    markdown = (out_dir / MARKDOWN_NAME).read_text()
    assert "| evil \\| name next |" in markdown


def test_not_met_is_printed_with_nothing_softening_it(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["operators"] = world.payload["operators"][:1]
    world.payload["totals"] = {"external_agents": 1, "unique_operator_wallets": 1, "settled_external_workflows": 2}
    world.payload["met"] = {k: False for k in world.payload["met"]}
    world.payload["degraded"] = True
    world.payload["unreadable_agents"] = ["delta"]
    code, out, _ = run(world, tmp_path)
    assert code == EXIT_OK
    lines = out.splitlines()
    not_met = [line for line in lines if "NOT MET" in line and line.startswith("- **")]
    assert not_met == [
        "- **Externally operated agents: 1 of 2 — NOT MET**",
        "- **Unique operator wallets: 1 of 2 — NOT MET**",
        "- **Workflows routed to external agents and settled: 2 of 3 — NOT MET**",
    ]


def test_json_carries_the_recount_the_links_and_every_check(tmp_path: Path) -> None:
    world = healthy_world()
    _, _, out_dir = run(world, tmp_path)
    doc: dict[str, Any] = json.loads((out_dir / JSON_NAME).read_text())
    assert doc["schema"] == SCHEMA
    assert doc["network"] == "testnet"
    assert doc["exit_code"] == EXIT_OK
    assert doc["sources"]["api"] == API + "/api/ecosystem/adoption"
    assert doc["targets"] == doc["totals"] == doc["api_totals"]
    assert doc["met_lines"] == {k: "MET" for k in doc["targets"]}
    assert doc["contracts"]["payment_escrow"] == {"id": ESCROW, "explorer": f"{EXPLORER}/contract/{ESCROW}"}
    assert doc["team_register"]["accounts"] == 2 and len(doc["team_register"]["sha256"]) == 64
    first = doc["operators"][0]
    assert first["owner_explorer"] == f"{EXPLORER}/account/{OP1}"
    wf = first["agents"][0]["settled_workflows"][0]
    assert wf["explorer"] == f"{EXPLORER}/tx/{wf['tx_hash']}"
    assert wf["read_from"] == "rpc" and wf["counted"] is True and wf["amount_stroops"] == 100_000
    assert wf["ledger"] == world.txs[wf["tx_hash"]].ledger
    assert doc["excluded"][0]["owner"] == PLATFORM
    names = {(c["subject"], c["check"]) for c in doc["checks"]}
    assert ("agent alpha", "owner_of") in names
    assert ("totals", "total:settled_external_workflows") in names
    assert all(c["ok"] is not False for c in doc["checks"])


def test_failed_run_records_its_failures_in_the_json(tmp_path: Path) -> None:
    world = healthy_world()
    world.registry["alpha"]["owner"] = OP2
    code, _, out_dir = run(world, tmp_path)
    doc = json.loads((out_dir / JSON_NAME).read_text())
    assert doc["exit_code"] == code != EXIT_OK
    failed = [c for c in doc["checks"] if c["ok"] is False]
    assert {"subject": "agent alpha", "check": "owner_of", "ok": False, "kind": "contradicted"}.items() <= failed[
        0
    ].items()
    assert doc["totals"]["external_agents"] == 1
    assert "## Failed checks" in (out_dir / MARKDOWN_NAME).read_text()
