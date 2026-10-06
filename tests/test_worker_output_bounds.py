"""Structured workers on Claude ask for an unbounded draft and fit it in code.

Claude's structured outputs enforce a schema's shape but not its string
lengths, list sizes or number ranges: `transform_schema` moves those into the
field descriptions, and `app/llm/claude.py` validates them after the call — so
a bounded schema turns a long-but-good answer into a failed step. The live eval
of 2026-10-06 caught exactly that: research.pro failed 3/3 because Sonnet 5.5
wrote 223–236-character claims against a 200 cap and a 421-character summary
against 300 (evals/orchestrator/reports/2026-10-06/workers/).

So each structured worker sends a `*Draft` schema with no hard bounds and
normalizes the draft into its unchanged output model: text trimmed at a
sentence (else a word) boundary without cutting a bracketed citation, lists
capped, numbers clamped. Too FEW items cannot be fixed without inventing
content, so that stays the step's `invalid_output` failure.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.agents.registry import WORKERS
from app.agents.workers import copywrite
from app.agents.workers.bounds import trim_text
from app.agents.workers.claude_step import ModelStepError
from app.config import settings
from app.llm.testing import FakeClaude

INTENT = "a landing page for a neighbourhood bike repair co-op"
HARD_BOUNDS = ("{maxLength", "{minLength", "{maxItems", "{minItems", "{maximum", "{minimum")


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def run(agent_id: str) -> dict[str, Any]:
    return asyncio.run(WORKERS[agent_id].run(INTENT, "why"))


def _sentence(n: int, i: int = 0) -> str:
    """A sentence of exactly `n` characters, ending in a full stop."""
    head = f"Claim {i} says "
    return head + "x" * (n - len(head) - 1) + "."


# ── trim_text ───────────────────────────────────────────────────────────────


def test_text_within_the_limit_is_returned_unchanged() -> None:
    assert trim_text("Short and sweet.", 200) == "Short and sweet."
    assert trim_text("a" * 200, 200) == "a" * 200


def test_an_over_long_text_keeps_its_whole_sentences() -> None:
    text = "Co-ops cut repair costs by sharing tools. Members learn by doing. " + "Then " + "word " * 60
    out = trim_text(text, 80)
    assert out == "Co-ops cut repair costs by sharing tools. Members learn by doing."


def test_with_no_sentence_to_keep_it_cuts_at_a_word_and_says_so() -> None:
    out = trim_text("one two three four five six seven eight nine ten", 20)
    assert out == "one two three four…" and len(out) <= 20


def test_a_cut_never_lands_inside_a_bracketed_citation() -> None:
    text = (
        "Shared tool libraries lower barriers to entry (Smith and Jones, Community Repair Review, 2021) for new riders"
    )
    out = trim_text(text, 70)
    assert out.endswith("entry…")
    assert "(" not in out and len(out) <= 70


def test_a_sentence_end_inside_brackets_is_not_a_sentence_end() -> None:
    text = "Repair cafes help [see e.g. the 2019 survey. It covers 40 cities] and grow every year in most regions"
    out = trim_text(text, 60)
    assert "[" not in out or "]" in out
    assert len(out) <= 60


def test_a_single_unbreakable_word_is_still_bounded() -> None:
    out = trim_text("x" * 500, 50)
    assert len(out) == 50 and out.endswith("…")


# ── every structured worker sends a schema without hard bounds ──────────────


def _replies() -> dict[str, Any]:
    return {
        "agt_01h8": {
            "hero_headline": "Fix it together",
            "hero_subtitle": "s",
            "sections": [{"title": f"T{i}", "body": "b"} for i in range(3)],
        },
        "agt_02k2": {
            k: "#000000"
            for k in ("bg", "surface", "surface_2", "border", "text", "muted", "primary", "accent", "danger")
        }
        | {"family_ui": "Inter, system-ui", "family_display": "Inter, system-ui"},
        "agt_04m1": {"summary": "s", "findings": [], "cvss_estimate": 1.0},
        "agt_05x7": {"keywords": ["k"], "audiences": ["a"], "summary": "s"},
        "agt_09l5": {
            "findings": [{"claim": f"c{i}", "confidence": 0.5} for i in range(3)],
            "sources": ["s"],
            "summary": "s",
        },
    }


@pytest.mark.parametrize("agent_id", ["agt_01h8", "agt_02k2", "agt_04m1", "agt_05x7", "agt_09l5"])
def test_no_structured_worker_sends_a_bound_claude_would_be_failed_on(agent_id: str, claude: FakeClaude) -> None:
    claude.reply(_replies()[agent_id])
    run(agent_id)
    schema = json.dumps(claude.calls[0].json_schema)
    assert not [b for b in HARD_BOUNDS if b in schema], schema


# ── research.pro: the live eval's failure, replayed ─────────────────────────


def _research(findings: int = 6, claim_chars: int = 236, summary_chars: int = 421) -> dict[str, Any]:
    return {
        "findings": [
            {"claim": _sentence(120, i) + " " + _sentence(claim_chars - 121, i), "confidence": 1.3 if i == 0 else 0.6}
            for i in range(findings)
        ],
        "sources": [f"source {i}" for i in range(8)],
        "summary": " ".join(_sentence(100, i) for i in range(5))[:summary_chars],
    }


def test_research_pro_fits_the_live_evals_long_answer_into_its_contract(claude: FakeClaude) -> None:
    claude.reply(_research())
    out = run("agt_09l5")

    assert all(len(f["claim"]) <= 200 for f in out["findings"])
    # Whole sentences kept: the 120-character first sentence of each claim.
    assert all(f["claim"].endswith(".") for f in out["findings"])
    assert len(out["summary"]) <= 300 and out["summary"].endswith(".")
    assert len(out["sources"]) == 6
    assert out["findings"][0]["confidence"] == 1.0
    assert out["counts"] == {"findings": 6, "sources": 6}
    assert set(out) == {"summary", "findings", "sources", "counts", "source"}


def test_research_pro_caps_findings_at_six(claude: FakeClaude) -> None:
    claude.reply(_research(findings=9))
    assert len(run("agt_09l5")["findings"]) == 6


def test_research_pro_with_too_few_findings_is_still_invalid(claude: FakeClaude) -> None:
    """Two findings cannot become three without inventing one."""
    claude.reply(_research(findings=2))
    with pytest.raises(ModelStepError) as info:
        run("agt_09l5")
    assert info.value.rule == "invalid_output"


def test_research_pro_drops_blank_findings_before_counting(claude: FakeClaude) -> None:
    reply = _research(findings=3)
    reply["findings"][1]["claim"] = "   "
    claude.reply(reply)
    with pytest.raises(ModelStepError):
        run("agt_09l5")


# ── sol-audit, seo.brief, copywrite: the same trap, the same fix ────────────


def test_sol_audit_fits_long_rationales_extra_findings_and_an_out_of_range_score(claude: FakeClaude) -> None:
    claude.reply(
        {
            "summary": " ".join(_sentence(100, i) for i in range(4)),
            "findings": [
                {"severity": "high", "title": f"Issue {i}", "rationale": _sentence(150, i) + " " + _sentence(150, i)}
                for i in range(8)
            ],
            "cvss_estimate": 12.5,
        }
    )
    out = run("agt_04m1")
    assert len(out["summary"]) <= 280
    assert len(out["findings"]) == 6 and all(len(f["rationale"]) <= 240 for f in out["findings"])
    assert out["cvss_estimate"] == 10.0


def test_seo_brief_caps_keywords_and_audiences(claude: FakeClaude) -> None:
    claude.reply({"keywords": [f"k{i}" for i in range(15)], "audiences": [f"a{i}" for i in range(7)], "summary": "s"})
    out = run("agt_05x7")
    assert (len(out["keywords"]), len(out["audiences"])) == (12, 5)


def test_copywrite_fits_long_bodies_and_extra_sections(claude: FakeClaude) -> None:
    claude.reply(
        {
            "hero_headline": "Fix it together",
            "hero_subtitle": "Free on Saturdays.",
            "sections": [{"title": f"T{i}", "body": _sentence(200, i) + " " + _sentence(200, i)} for i in range(7)],
        }
    )
    out = run("agt_01h8")
    assert len(out["sections"]) == 5 and all(len(s["body"]) <= 280 for s in out["sections"])


def test_copywrite_with_one_section_is_still_invalid(claude: FakeClaude) -> None:
    claude.reply({"hero_headline": "h", "hero_subtitle": "s", "sections": [{"title": "t", "body": "b"}]})
    with pytest.raises(ModelStepError) as info:
        run("agt_01h8")
    assert info.value.rule == "invalid_output"


# ── copywrite never invents facts ───────────────────────────────────────────


def test_copywrite_tells_the_model_never_to_invent_facts(claude: FakeClaude) -> None:
    """The live eval's copy promised "Results in 8 weeks or your money back" and
    "Join 500+ members" — neither was in the request. The rule must reach the
    model as part of its instructions (and stays in the system prompt, never
    in the untrusted turn)."""
    claude.reply(_replies()["agt_01h8"])
    run("agt_01h8")
    system = claude.calls[0].system.casefold()
    for kind in ("guarantee", "price", "statistic", "testimonial", "customer count", "award"):
        assert kind in system, kind
    assert "never invent" in system
    assert "[placeholder" in system
    assert system == copywrite.INSTRUCTIONS.casefold()
