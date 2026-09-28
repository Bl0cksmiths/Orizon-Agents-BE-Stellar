"""What the adoption report re-states from elsewhere, pinned to its source.

The verifier never imports `app/` at runtime, so the few facts it re-states
(the stroop conversion, the SOW targets, a placeholder simulation source) are
pinned here instead, where a drift fails the suite rather than a report.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from stellar_sdk import StrKey

from app.stellar.client import usdc_to_i128
from scripts.adoption_report.chain import SIMULATION_SOURCE
from scripts.adoption_report.config import TARGETS, normalize_api_base, usdc_to_stroops

PACKAGE = Path(__file__).resolve().parent.parent / "scripts" / "adoption_report"


@pytest.mark.parametrize("amount", [0.0, 0.0000001, 0.0000057, 0.0029, 0.001, 0.01, 0.012, 0.054, 1.5, 10_000.0])
def test_stroop_conversion_matches_the_backend(amount: float) -> None:
    assert usdc_to_stroops(amount) == usdc_to_i128(amount)


def test_sow_targets_are_the_story_s() -> None:
    assert TARGETS == {"external_agents": 2, "unique_operator_wallets": 2, "settled_external_workflows": 3}


def test_simulation_source_is_a_public_key_and_nothing_more() -> None:
    assert StrKey.is_valid_ed25519_public_key(SIMULATION_SOURCE)


def test_package_never_imports_app_or_the_lifecycle_harness() -> None:
    for source in PACKAGE.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("app", "scripts.lifecycle")), f"{source.name} imports {name}"


@pytest.mark.parametrize(
    ("raw", "base"),
    [("https://orizons.xyz", "https://orizons.xyz"), ("https://orizons.xyz/api/", "https://orizons.xyz")],
)
def test_api_base_is_normalized(raw: str, base: str) -> None:
    assert normalize_api_base(raw) == base


def test_api_base_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        normalize_api_base("orizons.xyz")
