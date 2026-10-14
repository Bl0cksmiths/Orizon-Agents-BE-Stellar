"""Characterization tests for the adoption Markdown's empty-table rows.

The healthy world fills every table; these claims leave one empty at a time:
an operator claimed with no agents, no workflow claimed anywhere, and no
excluded wallet listed. Each must still print its placeholder row.
"""

from __future__ import annotations

import io
from pathlib import Path

from scripts.adoption_report.cli import main
from scripts.adoption_report.fakes import API, ESCROW, HORIZON, OP2, REGISTRY, RPC, FakeWorld, healthy_world
from scripts.adoption_report.report import MARKDOWN_NAME


def _markdown(world: FakeWorld, tmp_path: Path) -> list[str]:
    out_dir = tmp_path / "evidence"
    main(
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
            str(FakeWorld.write_register(tmp_path / "team.json")),
            "--out-dir",
            str(out_dir),
        ],
        transport=world.transport(),
        stream=io.StringIO(),
        sleep=lambda _s: None,
        now=lambda: 1_790_000_200.0,
    )
    return (out_dir / MARKDOWN_NAME).read_text().splitlines()


def test_an_operator_claimed_without_agents_gets_a_dash_row(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["operators"][1]["agents"] = []
    lines = _markdown(world, tmp_path)
    owner = f"[`{OP2[:6]}…{OP2[-4:]}`](https://stellar.expert/explorer/testnet/account/{OP2})"
    assert f"| OP-2 | {owner} | — | — | — | — | no |" in lines


def test_no_workflow_claimed_anywhere_prints_none_claimed(tmp_path: Path) -> None:
    world = healthy_world()
    for operator in world.payload["operators"]:
        for agent in operator["agents"]:
            agent["settled_workflows"] = []
    lines = _markdown(world, tmp_path)
    assert "| — | — | none claimed | — | — | — | — | — |" in lines


def test_no_excluded_wallet_prints_none_listed(tmp_path: Path) -> None:
    world = healthy_world()
    world.payload["excluded"] = []
    lines = _markdown(world, tmp_path)
    assert "| — | — | — | none listed |" in lines
