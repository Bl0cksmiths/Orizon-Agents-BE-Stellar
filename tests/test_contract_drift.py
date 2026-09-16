"""Tests for the contract-address drift check (story 3.07).

Almost every assertion here runs against pure functions over text. That is the
point of splitting "parse and compare" from "find files and exit": the failure
modes that actually matter — an id that drifted, an id that vanished, a
canonical map that is not there at all — are cheap enough to cover exhaustively
when none of them needs a filesystem staged first.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from scripts.check_contract_drift import (
    CONTRACTS,
    MISSING,
    DriftCheckError,
    NetworkMap,
    candidate_dirs,
    compare,
    find_contracts_dir,
    load_sources,
    main,
    parse_canonical,
    parse_env_example,
    parse_render_yaml,
)

TESTNET_IDS: dict[str, str] = {name: f"C{name.upper().replace('_', '')}TESTNET" for name in CONTRACTS}
MAINNET_IDS: dict[str, str] = {name: f"C{name.upper().replace('_', '')}MAINNET" for name in CONTRACTS}


def _sources() -> dict[str, NetworkMap]:
    """Three sources that all agree — the shape the repo is in today.

    `render.yaml` names only mainnet because that is all it configures; the
    check must treat that silence as correct rather than as a gap.
    """
    return {
        "canonical": {"testnet": dict(TESTNET_IDS), "mainnet": dict(MAINNET_IDS)},
        ".env.example": {"testnet": dict(TESTNET_IDS), "mainnet": dict(MAINNET_IDS)},
        "render.yaml": {"mainnet": dict(MAINNET_IDS)},
    }


def _env_example(live: tuple[str, Mapping[str, str]], commented: tuple[str, Mapping[str, str]]) -> str:
    """Render an env file the way .env.example is written: one network's ids
    live, the other's commented out, each block labelled by its own
    STELLAR_NETWORK line."""
    lines = ["# Stellar", f"STELLAR_NETWORK={live[0]}"]
    lines += [f"STELLAR_{name.upper()}={value}" for name, value in live[1].items()]
    lines += ["", "# Uncomment to switch networks", f"# STELLAR_NETWORK={commented[0]}"]
    lines += [f"# STELLAR_{name.upper()}={value}" for name, value in commented[1].items()]
    return "\n".join(lines) + "\n"


def _render_yaml(ids: Mapping[str, str], network: str = "mainnet") -> str:
    lines = [
        "services:",
        "  - type: web",
        "    envVars:",
        # A secret carries `sync: false` and no value — the scanner has to walk
        # past it without pairing the next value with this key.
        "      - key: STELLAR_SIGNING_KEY",
        "        sync: false",
        "      - key: STELLAR_NETWORK",
        f"        value: {network}",
        "      - key: STELLAR_NETWORK_PASSPHRASE",
        '        value: "Public Global Stellar Network ; September 2015"',
    ]
    for name, value in ids.items():
        lines += [f"      - key: STELLAR_{name.upper()}", f"        value: {value}"]
    return "\n".join(lines) + "\n"


def _canonical_json(network: str, ids: Mapping[str, str]) -> str:
    # Shaped like the real address book, extra keys and all, so the parser is
    # exercised against a map it has to pick five values out of.
    return json.dumps({"network": network, "admin": "GADMIN", "asset": "native", **ids}, indent=2)


# ── comparison ───────────────────────────────────────────────────────────────


def test_sources_that_agree_pass_on_both_networks() -> None:
    report = compare(_sources())

    assert report.ok
    assert report.drifts == ()
    assert report.unverified == ()
    assert set(report.checked) == {"testnet", "mainnet"}
    body = "\n".join(report.lines())
    assert "PASS" in body
    assert "[OK]   testnet" in body
    assert "[OK]   mainnet" in body


def test_one_id_drifting_on_testnet_fails_and_names_the_contract() -> None:
    sources = _sources()
    sources[".env.example"]["testnet"]["reputation_ledger"] = "CSTALEREPUTATIONLEDGER"

    report = compare(sources)

    assert not report.ok
    assert [(d.network, d.contract) for d in report.drifts] == [("testnet", "reputation_ledger")]


def test_the_failure_message_carries_both_values_and_both_source_names() -> None:
    sources = _sources()
    sources[".env.example"]["testnet"]["reputation_ledger"] = "CSTALEREPUTATIONLEDGER"

    line = compare(sources).drifts[0].describe()

    # "Drift detected" would not be actionable. The line has to say which id, on
    # which network, per source, with the values that disagree.
    assert "reputation_ledger" in line
    assert "canonical=CREPUTATIONLEDGERTESTNET" in line
    assert ".env.example=CSTALEREPUTATIONLEDGER" in line
    assert line.startswith("testnet ")


def test_one_id_drifting_on_mainnet_fails_too() -> None:
    # The ticket's history is a testnet id, but mainnet is where orizons.xyz
    # points — a check that only covered testnet would leave this armed.
    sources = _sources()
    sources["render.yaml"]["mainnet"]["payment_escrow"] = "CWRONGESCROW"

    report = compare(sources)

    assert not report.ok
    drift = report.drifts[0]
    assert (drift.network, drift.contract) == ("mainnet", "payment_escrow")
    assert drift.values["canonical"] == MAINNET_IDS["payment_escrow"]
    assert drift.values["render.yaml"] == "CWRONGESCROW"


def test_a_contract_missing_from_one_source_is_drift_not_silence() -> None:
    sources = _sources()
    del sources["render.yaml"]["mainnet"]["attestation_registry"]

    report = compare(sources)

    assert not report.ok
    drift = report.drifts[0]
    assert (drift.network, drift.contract) == ("mainnet", "attestation_registry")
    assert drift.values["render.yaml"] == MISSING
    assert MISSING in drift.describe()


def test_a_contract_missing_from_every_source_is_still_drift() -> None:
    # Unanimous absence is the worst case, not the safe one: nothing verified
    # the id, and agreeing on nothing is not agreement.
    sources = _sources()
    for networks in sources.values():
        networks.get("testnet", {}).pop("asset_sac", None)

    report = compare(sources)

    assert not report.ok
    assert [(d.network, d.contract) for d in report.drifts] == [("testnet", "asset_sac")]


def test_a_network_only_one_source_describes_is_reported_unverified() -> None:
    sources = _sources()
    del sources[".env.example"]["mainnet"]
    del sources["render.yaml"]["mainnet"]

    report = compare(sources)

    assert not report.ok
    assert [u.network for u in report.unverified] == ["mainnet"]
    assert "nothing to compare it against" in "\n".join(report.lines())


def test_a_network_no_source_describes_is_reported_unverified() -> None:
    report = compare({"canonical": {"testnet": dict(TESTNET_IDS)}, ".env.example": {"testnet": dict(TESTNET_IDS)}})

    assert not report.ok
    assert [u.network for u in report.unverified] == ["mainnet"]
    assert "no source at all" in "\n".join(report.lines())


# ── parsing ──────────────────────────────────────────────────────────────────


def test_env_example_reads_the_live_and_commented_blocks_as_two_networks() -> None:
    parsed = parse_env_example(_env_example(("testnet", TESTNET_IDS), ("mainnet", MAINNET_IDS)))

    assert parsed == {"testnet": TESTNET_IDS, "mainnet": MAINNET_IDS}


def test_env_example_labels_blocks_by_their_own_network_line_not_by_position() -> None:
    # The file flips which block is live when the project switches networks; a
    # parser that assumed "the commented block is mainnet" would invert silently.
    parsed = parse_env_example(_env_example(("mainnet", MAINNET_IDS), ("testnet", TESTNET_IDS)))

    assert parsed == {"testnet": TESTNET_IDS, "mainnet": MAINNET_IDS}


def test_env_example_ignores_prose_that_merely_mentions_a_variable() -> None:
    text = _env_example(("testnet", TESTNET_IDS), ("mainnet", MAINNET_IDS))
    text += "# Backend's signing identity. In prod, set STELLAR_SIGNING_KEY\n"
    text += "# via host secrets. Same IDs live in STELLAR_ASSET_SAC notes.\n"

    assert parse_env_example(text) == {"testnet": TESTNET_IDS, "mainnet": MAINNET_IDS}


def test_env_block_without_a_network_line_cannot_be_attributed() -> None:
    text = "\n".join(f"STELLAR_{name.upper()}={value}" for name, value in TESTNET_IDS.items())

    with pytest.raises(DriftCheckError, match="STELLAR_NETWORK"):
        parse_env_example(text)


def test_a_key_set_twice_to_different_values_is_refused() -> None:
    text = _env_example(("testnet", TESTNET_IDS), ("mainnet", MAINNET_IDS))
    text += "STELLAR_AGENT_REGISTRY=CSOMETHINGELSE\n"

    with pytest.raises(DriftCheckError, match="twice"):
        parse_env_example(text)


def test_render_yaml_reads_values_and_walks_past_secrets() -> None:
    assert parse_render_yaml(_render_yaml(MAINNET_IDS)) == {"mainnet": MAINNET_IDS}


def test_canonical_json_is_read_by_its_declared_network() -> None:
    assert parse_canonical(_canonical_json("testnet", TESTNET_IDS), "canonical") == {"testnet": TESTNET_IDS}


def test_canonical_json_without_a_network_key_is_refused() -> None:
    with pytest.raises(DriftCheckError, match="network"):
        parse_canonical('{"reputation_ledger": "CX"}', "canonical")


def test_canonical_json_that_is_not_json_is_refused() -> None:
    with pytest.raises(DriftCheckError, match="not valid JSON"):
        parse_canonical("network: testnet", "canonical")


def test_public_and_mainnet_name_the_same_network() -> None:
    # stellar-sdk says `public` where this repo says `mainnet`; folding them
    # keeps one chain from being compared against itself as two.
    assert parse_canonical(_canonical_json("public", MAINNET_IDS), "canonical") == {"mainnet": MAINNET_IDS}


def test_an_unknown_network_name_is_refused() -> None:
    with pytest.raises(DriftCheckError, match="unknown network"):
        parse_canonical(_canonical_json("futurenet", MAINNET_IDS), "canonical")


# ── finding the files ────────────────────────────────────────────────────────


def test_a_missing_canonical_map_is_a_failure_that_names_where_it_looked() -> None:
    with pytest.raises(DriftCheckError) as caught:
        find_contracts_dir([Path("/nowhere/one"), Path("/nowhere/two")])

    message = str(caught.value)
    assert "/nowhere/one" in message
    assert "/nowhere/two" in message
    # The check must say it did not run, not go quiet. Silently passing when the
    # canonical map is absent is the exact failure this story is about.
    assert "failure, not a skip" in message


def test_main_exits_two_when_the_canonical_map_is_absent(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["/nowhere/at/all"])

    assert code == 2
    assert "CANNOT CHECK CONTRACT DRIFT" in capsys.readouterr().err


def test_an_explicit_path_is_never_silently_replaced_by_a_default() -> None:
    # Falling back from a path someone named would turn a typo into a green
    # check against some other clone.
    assert candidate_dirs(Path("/repo"), "/named/clone") == [Path("/named/clone")]


def test_default_candidates_include_the_ci_checkout_path() -> None:
    candidates = candidate_dirs(Path("/repo"), None)

    assert Path("/repo/contracts") in candidates
    assert len(candidates) > 1


def test_load_sources_wires_the_three_files_together(tmp_path: Path) -> None:
    repo_root = tmp_path / "backend"
    contracts_dir = tmp_path / "contracts"
    repo_root.mkdir()
    contracts_dir.mkdir()
    (contracts_dir / "addresses.json").write_text(_canonical_json("testnet", TESTNET_IDS))
    (contracts_dir / "addresses.mainnet.json").write_text(_canonical_json("mainnet", MAINNET_IDS))
    (repo_root / ".env.example").write_text(_env_example(("testnet", TESTNET_IDS), ("mainnet", MAINNET_IDS)))
    (repo_root / "render.yaml").write_text(_render_yaml(MAINNET_IDS))

    report = compare(load_sources(repo_root, contracts_dir))

    assert report.ok
    assert report.checked["mainnet"] == ("canonical", ".env.example", "render.yaml")


def test_a_canonical_file_holding_the_wrong_network_is_refused(tmp_path: Path) -> None:
    # Copying addresses.json over addresses.mainnet.json would otherwise make
    # mainnet agree with itself while pointing at testnet contracts.
    repo_root = tmp_path / "backend"
    contracts_dir = tmp_path / "contracts"
    repo_root.mkdir()
    contracts_dir.mkdir()
    (contracts_dir / "addresses.json").write_text(_canonical_json("testnet", TESTNET_IDS))
    (contracts_dir / "addresses.mainnet.json").write_text(_canonical_json("testnet", TESTNET_IDS))

    with pytest.raises(DriftCheckError, match="that file is the mainnet map"):
        load_sources(repo_root, contracts_dir)
