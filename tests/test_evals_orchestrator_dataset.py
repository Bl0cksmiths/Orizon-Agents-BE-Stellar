"""The labelled intent set (evals/orchestrator/dataset.jsonl) and its loader.

The set is only worth measuring against if it is balanced, inside the API's own
bounds, and labelled consistently; these tests hold it there as cases are
added, and pin the loader's refusals so a malformed row can never be scored.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from evals.orchestrator.dataset import (
    DATASET_PATH,
    MAX_INTENT_CHARS,
    TIERS,
    DatasetError,
    assign_splits,
    load,
)


@pytest.fixture(scope="module")
def cases():
    return load()


def test_the_set_has_at_least_150_cases(cases):
    assert len(cases) >= 150


def test_no_verdict_dominates_the_set(cases):
    # A set that is mostly one label lets "always answer it" look accurate.
    counts = Counter(c.expected_verdict for c in cases)
    assert set(counts) == {"allow", "block", "needs_detail"}
    for verdict, n in counts.items():
        assert n / len(cases) <= 0.55, verdict
        assert n >= 30, verdict


def test_every_tier_is_represented_among_real_requests(cases):
    tiers = Counter(c.expected_tier for c in cases if c.expected_verdict == "allow")
    assert set(tiers) == set(TIERS)
    assert min(tiers.values()) >= 20


@pytest.mark.parametrize(
    "category",
    [
        "legit_low",
        "legit_moderate",
        "legit_complex",
        "legit_security",
        "injection_override",
        "injection_reveal",
        "injection_role",
        "injection_fence",
        "injection_hidden",
        "needs_detail_gibberish",
        "needs_detail_ping",
        "needs_detail_spam",
        "needs_detail_vague",
    ],
)
def test_each_category_has_enough_cases_to_report_on(cases, category):
    assert sum(c.category == category for c in cases) >= 6


def test_the_console_s_languages_are_covered(cases):
    langs = Counter(c.language for c in cases)
    assert langs["tl"] >= 10 and langs["taglish"] >= 8
    # Filipino in both directions: real requests AND injections, so a guard that
    # blocks everything non-English cannot score well.
    filipino = [c for c in cases if c.language in ("tl", "taglish")]
    assert {c.expected_verdict for c in filipino} == {"allow", "block", "needs_detail"}
    others = {lang for lang in langs if lang not in ("en", "tl", "taglish", "xx", "la")}
    assert len(others) >= 8


def test_borderline_security_asks_are_labelled_legitimate(cases):
    security = [c for c in cases if c.category == "legit_security"]
    assert security and all(c.expected_verdict == "allow" for c in security)
    assert any("reentrancy" in c.intent.lower() for c in security)


def test_injections_ride_on_kit_triggers_and_forged_fences(cases):
    hidden = [c for c in cases if c.category == "injection_hidden"]
    assert any("snake game" in c.intent.lower() for c in hidden)
    fence = [c for c in cases if c.category == "injection_fence"]
    assert any("END USER_INPUT" in c.intent for c in fence)
    assert any("AVAILABLE_AGENTS" in c.intent for c in fence)


def test_every_intent_fits_the_api_bound(cases):
    for c in cases:
        assert 3 <= len(c.intent.strip()) <= MAX_INTENT_CHARS, c.id


def test_only_real_requests_carry_a_tier(cases):
    for c in cases:
        assert (c.expected_tier is not None) == (c.expected_verdict == "allow"), c.id


def test_the_split_holds_out_part_of_every_category(cases):
    splits = assign_splits(cases)
    by_cat: dict[str, Counter[str]] = {}
    for c in cases:
        by_cat.setdefault(c.category, Counter())[splits[c.id]] += 1
    for cat, counts in by_cat.items():
        assert counts["test"] >= 1 and counts["train"] >= 1, cat
    assert 0.3 <= sum(v == "test" for v in splits.values()) / len(cases) <= 0.5


def test_the_split_is_deterministic(cases):
    assert assign_splits(cases) == assign_splits(list(reversed(cases)))


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "cases.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def _row(**over):
    row = {
        "id": "x-001",
        "intent": "Write a tagline for my bakery.",
        "expected_verdict": "allow",
        "expected_tier": "low",
        "category": "legit_low",
        "language": "en",
        "notes": "",
    }
    row.update(over)
    return row


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([_row(), _row()], "duplicate id"),
        ([_row(), _row(id="x-002", intent="  write a TAGLINE for my bakery. ")], "same intent"),
        ([_row(expected_verdict="block")], "only allow cases carry a tier"),
        ([_row(expected_tier=None)], "needs expected_tier"),
        ([_row(expected_verdict="maybe")], "expected_verdict"),
        ([_row(intent="hi")], "outside the API"),
        ([_row(intent="x" * 501)], "outside the API"),
        ([{k: v for k, v in _row().items() if k != "notes"}], "missing notes"),
    ],
)
def test_the_loader_refuses_rows_that_would_mislead(tmp_path, rows, message):
    with pytest.raises(DatasetError, match=message):
        load(_write(tmp_path, rows))


def test_the_committed_file_is_one_json_object_per_line():
    for n, line in enumerate(DATASET_PATH.read_text(encoding="utf-8").splitlines(), 1):
        assert isinstance(json.loads(line), dict), n
