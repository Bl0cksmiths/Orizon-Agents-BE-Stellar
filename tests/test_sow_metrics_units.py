"""The SOW metrics generator's parts: decoding, paging, retries, parsing and the register (story 5.05)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.sow_metrics import chain
from scripts.sow_metrics.config import GUIDE_PAGE, normalize_base
from scripts.sow_metrics.fakes import BUYER1, JOB1, SIGNER, met_world
from scripts.sow_metrics.metrics import amount, demo_state
from scripts.sow_metrics.register import RegisterError
from scripts.sow_metrics.register import load as load_register
from tests.test_sow_metrics_run import run


@pytest.mark.parametrize(
    ("stroops", "text"),
    [(1_140_000, "0.114"), (540_000, "0.054"), (10_000_000, "1"), (1, "0.0000001"), (0, "0"), (-5_000_000, "-0.5")],
)
def test_amounts_read_in_whole_units(stroops: int, text: str) -> None:
    assert amount(stroops) == text


@pytest.mark.parametrize(
    ("raw", "stroops"),
    [("0.0540000", 540_000), ("12", 120_000_000), ("-1.5", -15_000_000), (None, 0), ("0.00000019", 1)],
)
def test_horizon_amounts_convert_exactly(raw: str | None, stroops: int) -> None:
    assert chain.units_to_stroops(raw) == stroops


def test_an_undecodable_argument_does_not_hide_the_operation() -> None:
    world = met_world()
    record = next(r for r in world.histories[SIGNER] if r["transaction_hash"] == world.marks["settle1"])
    record["parameters"][2]["value"] = "not base64 xdr"
    op = chain.operation_from_horizon(record)
    assert op.function == "settle" and op.args[0] is None and len(op.args) == 4


@pytest.mark.parametrize(
    ("html", "state", "seconds"),
    [
        ('<article data-demo="published"><time datetime="PT3M42S">3:42</time>', "published", 222),
        ('<article data-demo="published"><time dateTime="PT4M">4:00</time>', "published", 240),
        ('<article data-demo="published"><time datetime="PT200S">', "published", 200),
        ('<article data-demo="unpublished">', "unpublished", None),
        ('<time datetime="2026-09-30">', None, None),
        ("<html></html>", None, None),
    ],
)
def test_the_demo_marker_and_running_time_are_parsed(html: str, state: str | None, seconds: int | None) -> None:
    assert demo_state(html) == (state, seconds)


def test_a_long_history_is_read_across_pages(tmp_path: Path) -> None:
    world = met_world()
    for i in range(450):
        world.rate("alpha", f"{i:032x}", BUYER1, at="2026-09-25")
    out = run(world, tmp_path)
    assert out.raw()["summary"]["ratings_by_kind"] == {"auto": 453, "dispute": 1}
    assert out.world.calls.count(f"horizon /accounts/{SIGNER}/operations") == 3


def test_a_history_past_the_page_bound_is_a_failed_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chain, "MAX_OPERATION_PAGES", 1)
    world = met_world()
    for i in range(250):
        world.rate("alpha", f"{i:032x}", BUYER1, at="2026-09-25")
    out = run(world, tmp_path)
    assert out.metric("m05")["achieved"] == "Not measured"
    assert "history" in out.raw()["failures"]


def test_one_operation_seen_from_two_platform_keys_counts_once(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    transfers = out.raw()["summary"]["platform_transfers"]
    assert len(transfers) == 2  # the refund (seen twice) and the drill
    assert out.metric("m05")["achieved"] == "1"


def test_a_transient_status_is_retried_a_bounded_number_of_times(tmp_path: Path) -> None:
    world = met_world()
    world.api_down.add("/api/stellar/reputation/params")
    run(world, tmp_path)
    assert world.calls.count("api /api/stellar/reputation/params") == 4


def test_a_404_is_an_answer_and_is_not_retried(tmp_path: Path) -> None:
    world = met_world()
    world.pages[GUIDE_PAGE] = (404, "")
    run(world, tmp_path)
    assert world.calls.count(f"frontend {GUIDE_PAGE}") == 1


def test_the_dispute_rating_is_read_from_the_scorers_history(tmp_path: Path) -> None:
    out = run(met_world(), tmp_path)
    counted = out.raw_metric("m05")["counted"][0]
    assert counted["dispute_job_id"][:16] == JOB1[:16] and counted["dispute_job_id"] != JOB1


@pytest.mark.parametrize(
    ("raw", "api"),
    [("https://orizons.xyz/api/", "https://orizons.xyz"), ("https://orizons.xyz", "https://orizons.xyz")],
)
def test_the_api_base_takes_either_form(raw: str, api: str) -> None:
    assert normalize_base(raw, "--api", strip_api=True) == api


def test_a_relative_base_is_refused() -> None:
    with pytest.raises(ValueError, match="absolute"):
        normalize_base("orizons.xyz", "--frontend")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "cannot be read"),
        ("{", "is not JSON"),
        ('{"accounts": []}', "has no `wallets` list"),
        ('{"wallets": [{"address": "GNOPE"}]}', "no valid Stellar account"),
        ('{"wallets": []}', "names no Stellar account"),
    ],
)
def test_a_bad_register_is_refused_with_its_reason(tmp_path: Path, content: str | None, message: str) -> None:
    path = tmp_path / "team_wallets.json"
    if content is not None:
        path.write_text(content)
    with pytest.raises(RegisterError, match=message):
        load_register(path)
