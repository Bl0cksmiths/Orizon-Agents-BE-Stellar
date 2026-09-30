"""No outside operator's agent id, wallet or hash is published before their consent is recorded.

`docs/operators/friction-log.md` ("Rules") and the onboarding runbook bar
publishing an outside operator's identity until their consent is recorded.
The metrics block is pasted into the public evidence index, so by default
(`--withhold-external`) every proof link that names an outside operator is
replaced by one link to the Ecosystem page. `--publish-external` links them
once consent exists. The counts never change: only the links do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.sow_metrics.config import MARKDOWN_NAME, RAW_NAME
from scripts.sow_metrics.fakes import (
    ADMIN,
    BUYER1,
    BUYER2,
    DISPATCH,
    FRONTEND,
    OP1,
    OP2,
    REGISTER_ROLES,
    SIGNER,
    TEAM_BUYER,
    TEAM_OP,
    FakeWorld,
    job,
    met_world,
)
from scripts.sow_metrics.metrics import WITHHELD_LABEL
from scripts.sow_metrics.report import block_problems
from tests.test_sow_metrics_run import Outcome, run

OURS = {ADMIN, TEAM_OP, TEAM_BUYER, SIGNER, DISPATCH}
OUTSIDE_WALLETS = {OP1, OP2, BUYER1, BUYER2}
OUTSIDE_AGENTS = re.compile(r"\b(alpha|beta|gamma)\b")
G_ADDRESS = re.compile(r"\bG[A-Z2-7]{55}\b")
TX_HASH = re.compile(r"\b[0-9a-f]{64}\b")
ECOSYSTEM = FRONTEND + "/app/ecosystem"
NAMES_AN_OUTSIDER = ("m01", "m02", "m03", "m04", "m05", "m06")


def outside_hashes(world: FakeWorld) -> set[str]:
    """Every transaction an outside operator or buyer is party to: registrations, payments, the dispute, the refund."""
    hashes = {r["transaction_hash"] for who in OUTSIDE_WALLETS for r in world.histories.get(who, [])}
    return hashes | {world.marks[k] for k in ("settle1", "settle2", "settle3", "dispute", "refund")}


def published_text(out: Outcome) -> str:
    return json.dumps(out.block(), ensure_ascii=False) + (out.dir / MARKDOWN_NAME).read_text()


def ecosystem_links(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [link for link in entry["links"] if link["url"] == ECOSYSTEM]


@pytest.mark.parametrize("consent", [None, "--withhold-external"])
def test_no_outside_wallet_agent_id_or_hash_is_published(tmp_path: Path, consent: str | None) -> None:
    world = met_world()
    out = run(world, tmp_path, consent=consent)
    text = published_text(out)
    addresses = set(G_ADDRESS.findall(text))
    assert addresses, "the team's own wallets are still linked"
    assert addresses <= OURS, addresses - OURS
    assert OUTSIDE_AGENTS.search(text) is None, OUTSIDE_AGENTS.search(text)
    assert not set(TX_HASH.findall(text)) & outside_hashes(world)
    assert block_problems(out.block()) == []


def test_an_outside_buyers_payment_to_a_team_agent_is_withheld(tmp_path: Path) -> None:
    """The payment names no outside agent, but its hash is the outside buyer's transaction."""
    world = met_world()
    tx = world.settle_v2(BUYER2, [("qa_agent", 700_000)], job("outside-buyer"), at="2026-09-25")
    withheld = published_text(run(world, tmp_path / "w", consent=None))
    published = published_text(run(world.copy(), tmp_path / "p"))
    assert tx in published
    assert tx not in withheld


def test_each_row_that_named_an_outsider_links_the_ecosystem_page_once(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path, consent=None)
    for entry in out.block():
        links = ecosystem_links(entry)
        if entry["id"] in NAMES_AN_OUTSIDER:
            assert links == [{"label": WITHHELD_LABEL, "url": ECOSYSTEM, "kind": "page"}], entry["id"]
        else:
            assert links == [], entry["id"]
    assert "held back" in WITHHELD_LABEL and "consent" in WITHHELD_LABEL
    assert G_ADDRESS.search(WITHHELD_LABEL) is None and OUTSIDE_AGENTS.search(WITHHELD_LABEL) is None


def test_withholding_changes_the_links_and_nothing_else(tmp_path: Path) -> None:
    withheld = run(met_world(), tmp_path / "w", consent=None).block()
    published = run(met_world(), tmp_path / "p").block()
    for w, p in zip(withheld, published, strict=True):
        assert {k: v for k, v in w.items() if k != "links"} == {k: v for k, v in p.items() if k != "links"}
    team_links = [link for link in published[0]["links"] if "excluded" in link["label"]]
    assert team_links and all(link in withheld[0]["links"] for link in team_links)


def test_publish_external_links_every_outside_operator(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path, consent="--publish-external")
    text = published_text(out)
    assert {OP1, OP2} <= set(G_ADDRESS.findall(text))
    assert any(label.startswith("Registration of alpha by an outside operator's wallet") for label in out.labels("m01"))
    assert all(ecosystem_links(entry) == [] for entry in out.block())
    assert out.raw()["withheld_external"] is False
    assert "held back" not in out.out


def test_the_raw_json_keeps_them_and_says_they_were_withheld(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path, consent=None)
    raw = json.loads((out.dir / RAW_NAME).read_text())
    assert raw["withheld_external"] is True
    assert {c["agent_id"] for c in raw["metrics"][0]["counted"]} == {"alpha", "beta", "gamma"}
    assert "note: outside operators' agent ids, wallets and hashes are held back" in out.out
    assert "consent to publish them is recorded" in (out.dir / MARKDOWN_NAME).read_text()


def test_a_world_with_no_outsider_is_unchanged_by_withholding(tmp_path: Path) -> None:
    roles = {**REGISTER_ROLES, OP1: "k1", OP2: "k2", BUYER1: "b1", BUYER2: "b2"}
    register = FakeWorld.write_register(tmp_path / "r.json", roles)
    withheld = run(met_world(), tmp_path / "w", register=register, consent=None).block()
    published = run(met_world(), tmp_path / "p", register=register).block()
    assert withheld == published


def test_the_two_flags_cannot_be_combined(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        run(met_world(), tmp_path, "--publish-external", consent="--withhold-external")
    assert exc.value.code == 2
