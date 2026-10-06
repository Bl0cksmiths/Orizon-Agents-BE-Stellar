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


# ── code.critic ─────────────────────────────────────────────────────────────


def code(title: str = "Spoke", body: str = "DRAFT-HTML", **over: Any) -> dict[str, Any]:
    html = f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head><body>{body}</body></html>'
    return {
        "summary": f"{title} — a co-op site",
        "artifact": {
            "title": title,
            "summary": "A co-op site. Deferred: booking.",
            "entry": "index.html",
            "files": [{"path": "index.html", "language": "html", "content": html}],
            "preview_html": html,
        },
        "validator_violations": [],
        **over,
    }


def test_code_critic_sees_the_copy_and_design_intent_beside_the_draft(claude: FakeClaude) -> None:
    claude.reply(TAGGED_APP, purpose="worker.code.critic")
    run("agt_12r0", ctx(**{"copywrite.v3": copy_out(), "design.figma": design(), "code.gen": code()}))
    prompt = claude.calls_for("worker.code.critic")[0].user
    body = fenced_body(prompt)

    assert "Hero headline: COPY-HEADLINE Fix it together" in body
    assert "  --primary: #7C5CFF;" in body
    assert "Deferred: booking." in body  # the draft's own account of what it left out
    assert "DRAFT-HTML" not in body  # the HTML travels in its own fence, once
    assert prompt.count("DRAFT-HTML") == 1
    assert prompt.index("VIOLATIONS") < prompt.index(FENCE_BEGIN) < prompt.index("BEGIN DRAFT_HTML")
    assert prompt.rstrip().endswith("Return the improved CodeArtifact.")


def test_code_critic_reviews_the_latest_code_step(claude: FakeClaude) -> None:
    claude.reply(TAGGED_APP, purpose="worker.code.critic")
    first_pass = code(title="Polished", body="FIRST-PASS")
    context = ctx(**{"code.gen": code(body="OLD-DRAFT"), "code.critic": first_pass})
    assert WORKERS["agt_12r0"].upstream_sources(context)[0] == "code.critic"
    run("agt_12r0", context)
    prompt = claude.calls_for("worker.code.critic")[0].user
    assert "FIRST-PASS" in prompt
    assert "OLD-DRAFT" not in prompt


def _next_project() -> dict[str, Any]:
    out = code(title="Pricing", body="NEXT-PREVIEW")
    out["artifact"]["framework"] = "next"
    out["artifact"]["files"] = [
        {"path": "app/page.tsx", "language": "tsx", "content": "export default function Page() {}"},
        {"path": "components/Pricing.tsx", "language": "tsx", "content": "export function Pricing() {}"},
    ]
    return out


def test_code_critic_declines_a_next_project_instead_of_rewriting_it_as_html(claude: FakeClaude) -> None:
    from app.agents.workers.claude_step import ModelStepError
    from app.agents.workers.code_critic_worker import UNSUPPORTED_ARTIFACT

    context = ctx(**{"code.gen": code(body="OLD-HTML"), "copywrite.v3": copy_out(), "code.next": _next_project()})
    worker = WORKERS["agt_12r0"]
    assert worker.upstream_sources(context) == []  # nothing is reviewed, so nothing is used
    assert worker.step_model("moderate", context) is None  # and the trace names no model
    with pytest.raises(ModelStepError) as info:
        run("agt_12r0", context)
    assert info.value.rule == UNSUPPORTED_ARTIFACT
    assert claude.calls == []  # never handed to a model, never polished into the older HTML draft


def test_code_critic_on_a_baked_draft_names_the_draft_and_asks_no_model(claude: FakeClaude) -> None:
    context = ctx(**{"copywrite.v3": copy_out(), "code.gen": code(source="baked")})
    assert WORKERS["agt_12r0"].upstream_sources(context) == ["code.gen"]
    out = run("agt_12r0", context)
    assert out["source"] == "baked"
    assert claude.calls == []


def test_code_critic_with_no_draft_uses_nothing() -> None:
    assert WORKERS["agt_12r0"].upstream_sources(ctx(**{"copywrite.v3": copy_out()})) == []


# ── deploy.v0 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("outputs", "sealed_role", "title"),
    [
        ({"code.gen": code(title="Draft")}, "code.gen", "Draft"),
        ({"code.gen": code(title="Draft"), "code.critic": code(title="Polished")}, "code.critic", "Polished"),
        ({"code.next": code(title="NextApp")}, "code.next", "NextApp"),
        (
            {"code.gen": code(title="Draft"), "code.critic": {"summary": "no draft", "artifact": None}},
            "code.gen",
            "Draft",
        ),
        ({"code.gen": code(title="Draft"), "external.agt_op": code(title="Operator")}, "external.agt_op", "Operator"),
    ],
    ids=["code.gen", "critic-over-draft", "code.next", "critic-without-artifact", "operator-build"],
)
def test_deploy_seals_what_the_latest_code_step_produced(outputs: dict[str, Any], sealed_role: str, title: str) -> None:
    context = ctx(**outputs)
    assert WORKERS["agt_08j2"].upstream_sources(context) == [sealed_role]
    out = run("agt_08j2", context)
    assert out["summary"].startswith(f"sealed {title} · 1 file")
    assert out["preview_url"]


def test_deploy_with_no_build_seals_nothing_and_names_no_source() -> None:
    context = ctx(**{"copywrite.v3": copy_out()})
    assert WORKERS["agt_08j2"].upstream_sources(context) == []
    out = run("agt_08j2", context)
    assert out["preview_url"] is None
    assert out["files"] == 0


# ── the code briefs ─────────────────────────────────────────────────────────


def test_code_gens_claude_brief_says_how_to_build_from_the_upstream_block() -> None:
    from app.agents.workers import code_gen

    flat = " ".join(code_gen.CLAUDE_INSTRUCTIONS.split())
    assert "An UPSTREAM_OUTPUTS block (when present) holds what earlier agents in this pipeline produced" in flat
    assert "copy its `:root { --bg: …; --primary: …; }` block verbatim" in flat
    assert "use its hero headline, subtitle and section copy as the page's text" in flat
    assert "DESIGN_TOKENS" not in flat  # no section by that name reaches the Claude path any more


def test_code_critics_claude_brief_says_to_check_the_draft_against_the_upstream_intent() -> None:
    from app.agents.workers import code_critic

    flat = " ".join(code_critic.CLAUDE_INSTRUCTIONS.split())
    assert "UPSTREAM_OUTPUTS block (when present)" in flat
    assert "Restore any of them the draft drifted from" in flat
    assert "UPSTREAM_OUTPUTS" not in code_critic.INSTRUCTIONS  # the agno brief is pinned unchanged
