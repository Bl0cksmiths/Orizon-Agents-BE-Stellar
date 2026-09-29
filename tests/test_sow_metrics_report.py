"""The frozen metrics block, pinned exactly (story 5.05).

The frontend's `content/evidence/index.json` `metrics` array is pasted from
this block, so its shape is a contract: the keys and their order, the SOW's
wording character for character, a reason on every miss and none on a met
row, and links whose labels are words — never a bare hash or address — and
whose URLs are exactly the testnet explorer's.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.sow_metrics import metrics as metrics_module
from scripts.sow_metrics.cli import main
from scripts.sow_metrics.config import BLOCK_NAME, GUIDE_PAGE, MARKDOWN_NAME, RAW_NAME, SOW_ROWS
from scripts.sow_metrics.fakes import (
    ADMIN,
    API,
    BACKEND,
    ESCROW_V2,
    FRONTEND,
    GITHUB,
    HORIZON,
    LEDGER,
    RPC,
    FakeWorld,
    met_world,
)
from scripts.sow_metrics.report import block_problems, label_problem
from tests.test_sow_metrics_run import run

SOW_TEXT = [
    ("m01", "Adoption targets", "Externally-operated agents registered on Testnet", "≥ 2"),
    ("m02", "Adoption targets", "Unique external operator wallet addresses", "≥ 2"),
    ("m03", "Transaction targets", "Workflows routed to external agents & settled on Testnet", "≥ 3"),
    ("m04", "Transaction targets", "On-chain USDC settlements (charges) recorded", "≥ 3"),
    ("m05", "Transaction targets", "Dispute → partial-refund settlements", "≥ 1"),
    ("m06", "Technical milestones", "Permissionless AgentRegistry.register flow live on the dApp", "Yes"),
    ("m07", "Technical milestones", "Reputation-gated routing (reads avg_bps, applies a floor) live", "Yes"),
    ("m08", "Technical milestones", "Automated dispute window + partial-credit refund live", "Yes"),
    ("m09", "Technical milestones", 'Public "List your agent on Orizon" integration guide published', "Yes"),
    ("m10", "Technical milestones", "3–5 min demo video published", "Yes"),
    ("m11", "Technical milestones", "All source code released under MIT License", "Yes"),
]
MET_KEYS = ["id", "category", "metric", "target", "achieved", "status", "method", "links"]
MISS_KEYS = ["id", "category", "metric", "target", "achieved", "status", "reason", "method", "links"]


def _missed_world() -> FakeWorld:
    """Every metric missed, the way today's testnet misses them."""
    world = met_world()
    world.escrows[ESCROW_V2].ids.clear()
    for name in ("settle1", "settle2", "settle3", "dispute", "refund"):
        world.drop(world.marks[name])
    world.disputed.clear()
    world.pages = {}
    world.params = {"enabled": False, "floor_bps": 5500}
    world.repos = {}
    world.routes = set()
    return world


def test_the_sow_rows_are_verbatim() -> None:
    assert [(r.id, r.category, r.metric, r.target) for r in SOW_ROWS] == SOW_TEXT


def test_every_met_entry_has_exactly_the_frozen_keys(tmp_path: Path) -> None:
    block = run(met_world(), tmp_path).block()
    assert [(m["id"], m["category"], m["metric"], m["target"]) for m in block] == SOW_TEXT
    for entry in block:
        assert list(entry) == MET_KEYS, entry["id"]
        assert entry["status"] == "met"
        for link in entry["links"]:
            assert list(link)[:3] == ["label", "url", "kind"]
            assert set(link) <= {"label", "url", "kind", "tx_hash", "date"}


def test_every_missed_entry_carries_a_reason(tmp_path: Path) -> None:
    register = FakeWorld.write_register(tmp_path / "r.json", {ADMIN: "admin key"})
    world = _missed_world()
    world.agents = {k: {**v, "owner": ADMIN} for k, v in world.agents.items()}
    block = run(world, tmp_path, register=register).block()
    assert [m["status"] for m in block] == ["not_met"] * 11
    for entry in block:
        assert list(entry) == MISS_KEYS, entry["id"]
        assert entry["reason"].strip() and entry["reason"].endswith("."), entry


def test_one_met_entry_is_pinned_exactly(tmp_path: Path) -> None:
    entry = run(met_world(), tmp_path).metric("m07")
    assert entry == {
        "id": "m07",
        "category": "Technical milestones",
        "metric": "Reputation-gated routing (reads avg_bps, applies a floor) live",
        "target": "Yes",
        "achieved": "Yes",
        "status": "met",
        "method": (
            "Read the live reputation settings the router applies (whether the floor is on, and its value) from the "
            "deployed API. The router scores each agent from the ReputationLedger's on-chain average and leaves out "
            "any agent below the floor."
        ),
        "links": [
            {
                "label": "Live reputation settings (floor 5500 bps)",
                "url": "https://orizon.test/api/stellar/reputation/params",
                "kind": "doc",
            },
            {
                "label": "ReputationLedger contract (every rating)",
                "url": f"https://stellar.expert/explorer/testnet/contract/{LEDGER}",
                "kind": "contract",
            },
        ],
    }


def test_one_missed_entry_is_pinned_exactly(tmp_path: Path) -> None:
    world = met_world()
    world.pages[GUIDE_PAGE] = (404, "")
    assert run(world, tmp_path).metric("m09") == {
        "id": "m09",
        "category": "Technical milestones",
        "metric": 'Public "List your agent on Orizon" integration guide published',
        "target": "Yes",
        "achieved": "No",
        "status": "not_met",
        "reason": "The /guide/list-your-agent page answers HTTP 404: it has not been deployed yet.",
        "method": (
            "Opened the /guide/list-your-agent page on the live dApp with no login; published means it answers there. "
            "The page answered 404 when this ran, so it is not linked: a dead link would prove nothing."
        ),
        "links": [],
    }


def test_a_tx_link_is_exactly_the_testnet_explorer_with_its_hash_and_date(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    tx_links = [link for m in out.block() for link in m["links"] if link["kind"] == "tx"]
    assert tx_links
    for link in tx_links:
        assert link["url"] == f"https://stellar.expert/explorer/testnet/tx/{link['tx_hash']}"
        assert len(link["tx_hash"]) == 64 and int(link["tx_hash"], 16) >= 0
        assert len(link["date"]) == 10 and link["date"][4] == "-" and link["date"][7] == "-"
    refund = next(link for link in tx_links if link["label"].startswith("Refund of"))
    assert refund["tx_hash"] == out.world.marks["refund"]
    assert refund["date"] == "2026-09-24"


@pytest.mark.parametrize("builder", [met_world, _missed_world])
def test_labels_are_never_a_bare_hash_or_address(tmp_path: Path, builder: Any) -> None:
    block = run(builder(), tmp_path).block()
    labels = [link["label"] for m in block for link in m["links"]]
    assert labels
    for label in labels:
        assert label_problem(label) is None, label
    assert block_problems(block) == []


def test_unmeasured_rows_still_satisfy_the_shape(tmp_path: Path) -> None:
    world = met_world()
    world.simulate_down = True
    world.github_status = 403
    block = run(world, tmp_path).block()
    assert block_problems(block) == []
    assert sum(m["achieved"] == "Not measured" for m in block) == 7


@pytest.mark.parametrize(
    "label",
    [
        "a" * 64,
        f"Charge {'b' * 64} settled",
        "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
        "Owner GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV (team)",
        "the contract CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI",
        "",
        "   ",
    ],
)
def test_a_label_that_is_a_hash_or_address_is_caught(label: str) -> None:
    assert label_problem(label) is not None


def _mutations() -> list[tuple[str, Any]]:
    def swap_keys(b: list[dict[str, Any]]) -> None:
        e = b[0]
        b[0] = {"category": e["category"], **{k: v for k, v in e.items() if k != "category"}}

    def first_tx(b: list[dict[str, Any]]) -> dict[str, Any]:
        return next(link for m in b for link in m["links"] if link["kind"] == "tx")

    return [
        ("bare hash label", lambda b: first_tx(b).update(label=first_tx(b)["tx_hash"])),
        ("address in label", lambda b: b[1]["links"][0].update(label=f"owner {ADMIN}")),
        ("mainnet url", lambda b: first_tx(b).update(url=first_tx(b)["url"].replace("/testnet/", "/public/"))),
        ("http url", lambda b: b[9]["links"][0].update(url="http://orizons.xyz/demo")),
        ("hash mismatch", lambda b: first_tx(b).update(tx_hash="c" * 64)),
        ("tx without hash", lambda b: first_tx(b).pop("tx_hash")),
        ("bad date", lambda b: first_tx(b).update(date="24/09/2026")),
        ("bad kind", lambda b: b[0]["links"][0].update(kind="explorer")),
        ("missing reason", lambda b: b[8].pop("reason")),
        ("reason on a met row", lambda b: b[6].update(reason="because")),
        ("reworded metric", lambda b: b[4].update(metric="Dispute -> partial-refund settlements")),
        ("reworded target", lambda b: b[0].update(target=">= 2")),
        ("unknown status", lambda b: b[0].update(status="partial")),
        ("empty achieved", lambda b: b[0].update(achieved="")),
        ("key order", swap_keys),
        ("a row missing", lambda b: b.pop()),
    ]


@pytest.mark.parametrize(("name", "mutate"), _mutations(), ids=[n for n, _ in _mutations()])
def test_the_shape_check_has_teeth(tmp_path: Path, name: str, mutate: Any) -> None:
    world = met_world()
    world.pages[GUIDE_PAGE] = (404, "")
    block = run(world, tmp_path).block()
    assert block_problems(block) == []
    broken = copy.deepcopy(block)
    mutate(broken)
    assert block_problems(broken), name


def test_the_generator_refuses_to_write_a_block_that_breaks_the_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = metrics_module.m09

    def leaky(snap: Any, rules: Any, pending: Any = None) -> Any:
        m = real(snap, rules, pending)
        m.links[0] = metrics_module.Link(f"guide by {ADMIN}", m.links[0].url, "page")
        return m

    monkeypatch.setattr(metrics_module, "m09", leaky)
    with pytest.raises(ValueError, match="contains a Stellar address"):
        run(met_world(), tmp_path)
    assert not (tmp_path / "metrics" / BLOCK_NAME).exists()


def test_the_block_is_byte_identical_across_runs(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    first = (run(met_world(), tmp_path / "a").dir / BLOCK_NAME).read_bytes()
    stream = io.StringIO()
    reg = FakeWorld.write_register(tmp_path / "reg.json")
    main(
        [
            *("--api", API, "--backend", BACKEND, "--frontend", FRONTEND, "--rpc-url", RPC),
            *("--horizon-url", HORIZON, "--github-api", GITHUB, "--team-register", str(reg)),
            *("--out-dir", str(tmp_path / "b"), "--publish-external"),
        ],
        transport=met_world().transport(),
        stream=stream,
        sleep=lambda _s: None,
        now=lambda: 1_900_000_000.0,  # a different clock: the block carries no time
    )
    assert (tmp_path / "b" / BLOCK_NAME).read_bytes() == first
    assert b"1790000000" not in first and b"1900000000" not in first


def test_the_markdown_tables_every_metric(tmp_path: Path) -> None:
    world = met_world()
    world.pages[GUIDE_PAGE] = (404, "")
    md = (run(world, tmp_path).dir / MARKDOWN_NAME).read_text()
    assert md.startswith("# SOW §6.3 success metrics (story 5.05)\n\n**10 of 11 met.** (exit 0)")
    assert "| # | Category | Metric (SOW) | Target | Achieved | Status |" in md
    row = '| m09 | Technical milestones | Public "List your agent on Orizon" integration guide published | Yes | No |'
    assert row + " **Not met** |" in md
    assert "- **Why not:** The /guide/list-your-agent page answers HTTP 404" in md
    assert md.count("\n## m") == 11


def test_the_raw_json_lists_every_counted_and_excluded_item(tmp_path: Path) -> None:
    raw = json.loads((run(met_world(), tmp_path).dir / RAW_NAME).read_text())
    assert raw["network"] == "testnet" and raw["exit_code"] == 0
    assert raw["generated_utc"] == "2026-09-21T14:13:20Z"
    by_id = {m["id"]: m for m in raw["metrics"]}
    assert len(by_id["m01"]["counted"]) == 3 and len(by_id["m01"]["excluded"]) == 3
    assert len(by_id["m04"]["counted"]) == 4 and len(by_id["m04"]["excluded"]) == 1
    assert raw["summary"]["ratings_by_kind"] == {"auto": 3, "dispute": 1}
    assert raw["summary"]["ledger_lifetime_disputes"] == 1
    assert raw["failures"] == {}
