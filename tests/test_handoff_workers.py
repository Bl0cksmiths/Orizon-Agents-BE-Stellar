"""Each built-in worker builds on what the steps before it produced.

On FakeClaude only: the right upstream content reaches each worker's prompt,
inside the UPSTREAM_OUTPUTS fence and after the fenced request, and a step
with nothing upstream asks exactly what it asked before. The handoff itself
(map, bounds, scrub) is pinned in tests/test_upstream_handoff.py; the run
trace's "uses output from" line in tests/test_handoff_trace.py.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from app.agents.registry import WORKERS
from app.agents.workers.base import ModelWorker
from app.agents.workers.mock import MockWorker
from app.config import settings
from app.llm.testing import FakeClaude

INTENT = "a landing page for a neighbourhood bike repair co-op"
RATIONALE = "the buyer needs it"
FENCE_BEGIN = "BEGIN UPSTREAM_OUTPUTS"
FENCE_END = "END UPSTREAM_OUTPUTS"


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def research() -> dict[str, Any]:
    return {
        "summary": "Co-ops thrive on volunteer stands.",
        "findings": [{"claim": "RESEARCH-CLAIM riders want same-week fixes.", "confidence": 0.8}],
        "sources": ["community survey"],
        "source": "llm",
    }


def seo() -> dict[str, Any]:
    return {
        "summary": "Local repair intent.",
        "keywords": ["SEO-KEYWORD bike repair co-op"],
        "audiences": ["SEO-AUDIENCE commuters"],
        "source": "llm",
    }


def ctx(**outputs: Any) -> dict[str, Any]:
    return {"kit": None, "intent": INTENT, **outputs}


def fenced_body(prompt: str) -> str:
    """The text inside the prompt's UPSTREAM_OUTPUTS fence."""
    assert prompt.count(FENCE_BEGIN) == 1, prompt
    return prompt[prompt.index(FENCE_BEGIN) : prompt.index(FENCE_END)]


def run(agent_id: str, context: dict[str, Any] | None) -> dict[str, Any]:
    return asyncio.run(WORKERS[agent_id].run(INTENT, RATIONALE, context=context))


# ── the base hook ───────────────────────────────────────────────────────────


class _Reader(ModelWorker):
    id = "agt_test"
    name = "copywrite.v3"  # reads what copywrite reads
    real = True
    default_tier = "low"
    reads_upstream: ClassVar[bool] = True
    deterministic = False

    def _deterministic(self, context: dict[str, Any] | None) -> bool:
        return self.deterministic

    async def run(self, intent: str, rationale: str, context: Any = None, *, tier: Any = None) -> dict[str, Any]:
        return {}


def test_a_reading_worker_reports_the_roles_its_handoff_carries() -> None:
    assert _Reader().upstream_sources(ctx(**{"research.pro": research(), "seo.brief": seo()})) == [
        "seo.brief",
        "research.pro",
    ]


def test_a_deterministic_step_reports_no_upstream() -> None:
    reader = _Reader()
    reader.deterministic = True
    assert reader.upstream_sources(ctx(**{"seo.brief": seo()})) == []


def test_a_worker_that_does_not_read_upstream_never_claims_to() -> None:
    mock = MockWorker("agt_03d9", "code.next")  # code.next's role reads a lot; the mock reads nothing
    assert mock.upstream_sources(ctx(**{"seo.brief": seo(), "design.figma": {"palette": {"bg": "#000"}}})) == []
    assert not mock.handoff(ctx(**{"seo.brief": seo()}))


# ── copywrite.v3 ────────────────────────────────────────────────────────────


def _copy_reply() -> dict[str, Any]:
    return {
        "hero_headline": "Fix it together",
        "hero_subtitle": "Stands and tools, Saturdays.",
        "sections": [{"title": "Tools", "body": "b1"}, {"title": "Classes", "body": "b2"}],
    }


def test_copywrite_writes_from_the_seo_keywords_and_the_research(claude: FakeClaude) -> None:
    claude.reply(_copy_reply(), purpose="worker.copywrite.v3")
    run("agt_01h8", ctx(**{"research.pro": research(), "seo.brief": seo()}))
    prompt = claude.calls_for("worker.copywrite.v3")[0].user

    body = fenced_body(prompt)
    assert "SEO-KEYWORD bike repair co-op" in body
    assert "SEO-AUDIENCE commuters" in body
    assert "RESEARCH-CLAIM riders want same-week fixes." in body
    # The request's own fence comes first and the trusted ask comes last.
    assert prompt.index("BEGIN USER_INPUT") < prompt.index(FENCE_BEGIN)
    assert prompt.rstrip().endswith("Draft the copy.")
    assert "work its keywords in naturally" in prompt[: prompt.index(FENCE_BEGIN)]


def test_copywrite_with_nothing_upstream_asks_what_it_always_asked(claude: FakeClaude) -> None:
    from app.agents.workers.prompt_safety import worker_prompt

    claude.reply(_copy_reply(), purpose="worker.copywrite.v3")
    run("agt_01h8", ctx())
    assert claude.calls_for("worker.copywrite.v3")[0].user == worker_prompt(INTENT, RATIONALE, "Draft the copy.")


# ── seo.brief, research.pro, design.figma, sol-audit ────────────────────────


def ocr() -> dict[str, Any]:
    return {"summary": "read 2 lines", "text": "OCR-TEXT pragma solidity ^0.8.0;\ncontract Vault {}"}


def test_seo_brief_builds_on_the_research(claude: FakeClaude) -> None:
    claude.reply({"keywords": ["k"], "audiences": ["a"], "summary": "s"}, purpose="worker.seo.brief")
    run("agt_05x7", ctx(**{"research.pro": research()}))
    prompt = claude.calls_for("worker.seo.brief")[0].user
    assert "RESEARCH-CLAIM riders want same-week fixes." in fenced_body(prompt)
    assert prompt.rstrip().endswith("Return the SEO brief.")


def _research_reply() -> dict[str, Any]:
    return {
        "findings": [{"claim": f"c{i}", "confidence": 0.5} for i in range(3)],
        "sources": ["s"],
        "summary": "sum",
    }


def test_research_researches_the_extracted_text_and_the_audit(claude: FakeClaude) -> None:
    claude.reply(_research_reply(), purpose="worker.research.pro")
    audit = {"summary": "AUDIT-SUMMARY reentrancy", "findings": [], "cvss_estimate": 6.0}
    run("agt_09l5", ctx(**{"vision.ocr": ocr(), "sol-audit": audit}))
    body = fenced_body(claude.calls_for("worker.research.pro")[0].user)
    assert "OCR-TEXT pragma solidity ^0.8.0;" in body
    assert "AUDIT-SUMMARY reentrancy" in body
    assert body.index("vision.ocr") < body.index("sol-audit")  # map order: the source text first


def test_research_on_a_kit_asks_no_model_and_reads_nothing_upstream(claude: FakeClaude) -> None:
    from app.demo_kits import ALL_KITS

    kit = ALL_KITS[0].model_dump()
    worker = WORKERS["agt_09l5"]
    context = {"kit": kit, "intent": INTENT, "vision.ocr": ocr()}
    assert worker.upstream_sources(context) == []
    run("agt_09l5", context)
    assert claude.calls == []


def _tokens_reply() -> dict[str, Any]:
    keys = ["bg", "surface", "surface_2", "border", "text", "muted", "primary", "accent", "danger"]
    colours = dict.fromkeys(keys, "#123")
    return {**colours, "family_ui": "Inter, sans-serif", "family_display": "Inter, sans-serif"}


def test_design_takes_its_tone_from_the_brand_and_the_copy(claude: FakeClaude) -> None:
    claude.reply(_tokens_reply(), purpose="worker.design.figma")
    copy = {"hero": {"headline": "COPY-HEADLINE Fix it together", "subtitle": "sub"}, "sections": []}
    brand = {**seo(), "brand_name": "SEO-BRAND Spoke", "tagline": "Ride on"}
    run("agt_02k2", ctx(**{"seo.brief": brand, "copywrite.v3": copy, "research.pro": research()}))
    body = fenced_body(claude.calls_for("worker.design.figma")[0].user)
    assert "Brand name: SEO-BRAND Spoke" in body
    assert "Hero headline: COPY-HEADLINE Fix it together" in body
    assert body.index("seo.brief") < body.index("copywrite.v3") < body.index("research.pro")


def test_sol_audit_audits_the_extracted_contract_source(claude: FakeClaude) -> None:
    claude.reply({"summary": "s", "findings": [], "cvss_estimate": 1.0}, purpose="worker.sol-audit")
    run("agt_04m1", ctx(**{"vision.ocr": ocr(), "research.pro": research()}))
    body = fenced_body(claude.calls_for("worker.sol-audit")[0].user)
    assert "OCR-TEXT pragma solidity ^0.8.0;\ncontract Vault {}" in body  # line breaks kept: it is source
    assert "RESEARCH-CLAIM" in body


# ── code.gen ────────────────────────────────────────────────────────────────

TAGGED_APP = (
    "<artifact_title>Spoke</artifact_title><artifact_summary>A co-op site.</artifact_summary>"
    "<artifact_deferred>none</artifact_deferred><artifact_html><!doctype html><html><head>"
    '<meta charset="utf-8"><title>Spoke</title></head><body><main>APP</main></body></html></artifact_html>'
)


def design() -> dict[str, Any]:
    return {
        "summary": "palette",
        "palette": {
            "bg": "#0B0414",
            "surface": "#140A24",
            "surface_2": "#1D1033",
            "border": "#2A1A47",
            "text": "#F4F0FF",
            "muted": "#A99BC7",
            "primary": "#7C5CFF",
            "accent": "#22D3EE",
            "danger": "#F43F5E",
        },
        "typography": {"family_ui": "Inter, system-ui, sans-serif", "family_display": "Georgia, serif"},
        "css_vars": ":root { --bg: #0B0414; }",
        "source": "llm",
    }


def copy_out() -> dict[str, Any]:
    return {
        "summary": "COPY-HEADLINE",
        "hero": {"headline": "COPY-HEADLINE Fix it together", "subtitle": "COPY-SUBTITLE Saturdays."},
        "sections": [{"title": "Tools", "body": "COPY-BODY every tool you need."}],
    }


def test_code_gen_builds_from_the_design_tokens_copy_brand_and_research(claude: FakeClaude) -> None:
    claude.reply(TAGGED_APP, purpose="worker.code.gen")
    upstream = {"design.figma": design(), "copywrite.v3": copy_out(), "seo.brief": seo(), "research.pro": research()}
    run("agt_11c0", ctx(**upstream))
    prompt = claude.calls_for("worker.code.gen")[0].user
    body = fenced_body(prompt)

    assert "  --primary: #7C5CFF;" in body
    assert "Body font stack (family_ui): Inter, system-ui, sans-serif" in body
    assert "Hero headline: COPY-HEADLINE Fix it together" in body
    assert "Section — Tools: COPY-BODY every tool you need." in body
    assert "SEO-KEYWORD bike repair co-op" in body
    assert "RESEARCH-CLAIM" in body
    # Design first: the tokens are what code.gen must copy verbatim.
    assert body.index("design.figma") < body.index("copywrite.v3") < body.index("seo.brief")
    # Model-written upstream text never sits outside a fence any more.
    outside = prompt.replace(body, "")
    assert "#7C5CFF" not in outside
    assert "COPY-HEADLINE" not in outside
    assert prompt.rstrip().endswith("Return the CodeArtifact.")


def test_code_gen_on_a_kit_keeps_the_kit_sections_trusted_and_skips_its_duplicate_briefs() -> None:
    from app.agents.workers.code_gen import CodeGen
    from app.demo_kits import ALL_KITS

    kit = ALL_KITS[0].model_dump()
    context = {"kit": kit, "intent": INTENT, "seo.brief": seo(), "research.pro": research(), "design.figma": design()}
    prompt = CodeGen.build_prompt(INTENT, RATIONALE, context)

    body = fenced_body(prompt)
    assert "## BRAND" in prompt.replace(body, "")  # the kit's own, repo-owned and unfenced
    assert "  --primary: #7C5CFF;" in body
    assert "SEO-KEYWORD" not in prompt  # the kit's brand block is not sent twice
    assert "RESEARCH-CLAIM" not in prompt
    assert prompt.index("## BRAND") < prompt.index(FENCE_BEGIN)


def test_code_gen_reports_the_roles_it_was_handed() -> None:
    upstream = {"design.figma": design(), "copywrite.v3": copy_out(), "sol-audit": {"summary": "not read"}}
    assert WORKERS["agt_11c0"].upstream_sources(ctx(**upstream)) == ["design.figma", "copywrite.v3"]
