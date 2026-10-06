"""translate.42 on Claude, on FakeClaude only: upstream text segmented and fenced,
the request's own text when nothing is upstream, target languages decided by
the request, placeholders checked in code, every failure classed, and no
stand-in output off Claude."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.registry import WORKERS
from app.agents.workers import translate
from app.agents.workers.claude_step import ModelStepError
from app.agents.workers.context import upstream
from app.config import settings
from app.llm.errors import LLMUnavailable
from app.llm.testing import FakeClaude

INTENT = "Translate our landing page copy into Tagalog and Spanish"
RATIONALE = "the buyer serves Filipino and Spanish-speaking riders"
WORKER = translate.Translate42()

COPY = {
    "hero": {"headline": "Fix it together", "subtitle": "Hi {name}, tools are free on Saturdays."},
    "sections": [{"title": "Tools", "body": "Book at https://coop.example.com/book — [placeholder: hours]."}],
}


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def _lang(code: str, name: str, texts: dict[str, str]) -> dict[str, Any]:
    return {"code": code, "name": name, "segments": [{"id": k, "text": v} for k, v in texts.items()]}


TL = {
    "S1": "Ayusin natin nang sama-sama",
    "S2": "Hi {name}, libre ang mga gamit tuwing Sabado.",
    "S3": "Mga Gamit",
    "S4": "Mag-book sa https://coop.example.com/book — [placeholder: oras].",
}
ES = {
    "S1": "Arréglalo juntos",
    "S2": "Hola {name}, las herramientas son gratis los sábados.",
    "S3": "Herramientas",
    "S4": "Reserva en https://coop.example.com/book — [placeholder: horario].",
}


def _draft(*languages: dict[str, Any], source_text: str = "", source_language: str = "en") -> dict[str, Any]:
    return {
        "source_language": source_language,
        "source_text": source_text,
        "languages": list(languages) if languages else [_lang("tl", "Tagalog", TL), _lang("es", "Spanish", ES)],
    }


def run(context: dict[str, Any] | None = None, tier: Any = None, intent: str = INTENT) -> dict[str, Any]:
    return asyncio.run(WORKER.run(intent, RATIONALE, context=context, tier=tier))


def _failure(**kw: Any) -> ModelStepError:
    with pytest.raises(ModelStepError) as info:
        run(**kw)
    return info.value


def test_upstream_copy_is_translated_segment_by_segment_on_haiku(claude: FakeClaude) -> None:
    claude.reply(_draft())
    out = run(context={"copywrite.v3": COPY})

    (call,) = claude.calls
    assert (call.purpose, call.model, call.effort) == ("worker.translate.42", "claude-haiku-4-5", None)
    assert call.schema_name == "TranslateDraft" and call.system == translate.INSTRUCTIONS
    assert call.max_tokens == translate.MAX_TOKENS
    user = call.user
    assert user.index("END USER_INPUT") < user.index("BEGIN TEXT_TO_TRANSLATE") < user.index("[S1] (Hero headline)")
    assert user.index("[S4] (Section 1 body)") < user.index("END TEXT_TO_TRANSLATE")

    assert [t["language"] for t in out["translations"]] == ["tl", "es"]
    tl = out["translations"][0]
    assert tl["name"] == "Tagalog" and tl["lang"] == "tl"
    assert tl["text"] == "\n".join(TL.values())
    assert tl["segments"][1] == {
        "id": "S2",
        "role": "copywrite.v3",
        "label": "Hero subtitle",
        "source": "Hi {name}, tools are free on Saturdays.",
        "text": TL["S2"],
    }
    assert out["issues"] == []
    assert out["summary"] == "Translated 4 segment(s) from en into Tagalog, Spanish."
    assert out["counts"] == {"languages": 2, "segments": 4, "issues": 0}
    # What a later step receives through the handoff.
    assert "Ayusin natin" in upstream({"translate.42": out}, "copywrite.v3").text()


def test_the_steps_tier_picks_the_model(claude: FakeClaude) -> None:
    claude.reply(_draft())
    run(context={"copywrite.v3": COPY}, tier="moderate")
    assert (claude.calls[0].model, claude.calls[0].effort) == ("claude-sonnet-5-5", "medium")


def test_ads_and_extracted_text_are_segmented_with_their_labels() -> None:
    ads = {
        "ads": [
            {"headline": "Fix a flat", "primary_text": "Learn on Saturday.", "description": "", "cta": "LEARN_MORE"}
        ]
    }
    ocr = {"text": "OPEN\nMon–Fri 9–6", "language": "en"}
    segments, trimmed = translate.source_segments(list(upstream({"ads.meta": ads, "vision.ocr": ocr}, "translate.42")))
    assert [(s.id, s.role, s.label, s.text) for s in segments] == [
        ("S1", "ads.meta", "Ad 1 headline", "Fix a flat"),
        ("S2", "ads.meta", "Ad 1 text", "Learn on Saturday."),
        ("S3", "vision.ocr", "Text read from the image", "OPEN\nMon–Fri 9–6"),
    ]
    assert trimmed is False


def test_research_audit_and_seo_text_is_segmented_without_the_handoffs_annotations() -> None:
    context = {
        "research.pro": {"summary": "Co-ops thrive.", "findings": [{"claim": "Riders want fixes.", "confidence": 0.8}]},
        "sol-audit": {
            "summary": "One issue.",
            "findings": [{"severity": "high", "title": "Reentrancy", "rationale": "withdraw() calls out first"}],
        },
        "seo.brief": {"summary": "Local intent.", "tagline": "Fix it together", "keywords": ["bike repair"]},
    }
    segments, _ = translate.source_segments(list(upstream(context, "translate.42")))
    assert [(s.role, s.label, s.text) for s in segments] == [
        ("research.pro", "Research summary", "Co-ops thrive."),
        ("research.pro", "Finding 1", "Riders want fixes."),
        ("sol-audit", "Audit summary", "One issue."),
        ("sol-audit", "Audit finding 1", "Reentrancy"),
        ("sol-audit", "Audit finding 1 detail", "withdraw() calls out first"),
        ("seo.brief", "Tagline", "Fix it together"),
        ("seo.brief", "SEO summary", "Local intent."),
    ]
    assert not any("background, not facts" in s.text or "confidence" in s.text for s in segments)


def test_the_source_is_bounded_and_says_so(claude: FakeClaude, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(translate, "MAX_SOURCE_CHARS", 40)
    claude.reply(_draft(_lang("tl", "Tagalog", {"S1": "Ayusin natin"})))
    out = run(context={"copywrite.v3": COPY})
    assert [s["id"] for s in out["translations"][0]["segments"]] == ["S1"]
    assert out["issues"] == [{"problem": "source_trimmed", "kept_segments": 1}]


def test_with_nothing_upstream_the_requests_own_text_is_translated(claude: FakeClaude) -> None:
    intent = "Translate 'Welcome to our shop, {name}!' into French"
    claude.reply(
        _draft(
            _lang("fr", "French", {"R1": "Bienvenue dans notre boutique, {name} !"}),
            source_text="Welcome to our shop, {name}!",
        )
    )
    out = run(intent=intent)
    assert "No TEXT_TO_TRANSLATE block" in claude.calls[0].user
    assert "BEGIN TEXT_TO_TRANSLATE" not in claude.calls[0].user
    (fr,) = out["translations"]
    assert fr["segments"] == [
        {
            "id": "R1",
            "role": "request",
            "label": "Requested text",
            "source": "Welcome to our shop, {name}!",
            "text": "Bienvenue dans notre boutique, {name} !",
        }
    ]


def test_a_lost_placeholder_is_reported(claude: FakeClaude) -> None:
    broken = {**TL, "S2": "Hi, libre ang mga gamit tuwing Sabado.", "S4": "Mag-book — [placeholder: oras]."}
    claude.reply(_draft(_lang("tl", "Tagalog", broken)))
    out = run(context={"copywrite.v3": COPY})
    assert out["issues"] == [
        {"language": "tl", "problem": "placeholders", "segments": ["S2"], "tokens": ["{name}"]},
        {"language": "tl", "problem": "placeholders", "segments": ["S4"], "tokens": ["https://coop.example.com/book"]},
    ]


def test_an_incomplete_language_is_dropped_and_the_source_language_is_never_a_target(claude: FakeClaude) -> None:
    partial = {k: v for k, v in ES.items() if k != "S3"}
    claude.reply(_draft(_lang("en", "English", TL), _lang("es", "Spanish", partial), _lang("tl", "Tagalog", TL)))
    out = run(context={"copywrite.v3": COPY})
    assert [t["language"] for t in out["translations"]] == ["tl"]
    assert out["issues"] == [{"language": "es", "problem": "incomplete", "segments": ["S3"]}]


def test_at_most_four_languages(claude: FakeClaude) -> None:
    codes = ["tl", "es", "fr", "de", "ja", "ko"]
    claude.reply(_draft(*(_lang(c, c, TL) for c in codes)))
    out = run(context={"copywrite.v3": COPY})
    assert [t["language"] for t in out["translations"]] == codes[: translate.MAX_LANGUAGES]


@pytest.mark.parametrize(
    ("script", "kw", "rule"),
    [
        (
            lambda c: c.reply(_draft(_lang("fr", "French", {}))),
            {"context": {}, "intent": "Translate this please"},
            "no_input",
        ),
        (lambda c: c.reply({"source_language": "en", "source_text": "", "languages": []}), {}, "no_target_language"),
        (
            lambda c: c.reply(_draft(_lang("tl", "Tagalog", {"S1": "only one"}))),
            {"context": {"copywrite.v3": COPY}},
            "invalid_output",
        ),
        (lambda c: c.refuse(category="cyber", explanation="MODEL-PROSE"), {}, "model_refused"),
        (lambda c: c.truncate(partial='{"source_language": "en", "languages": [{"code'), {}, "model_truncated"),
        (lambda c: c.reply({"languages": "tl"}), {}, "invalid_output"),
        (lambda c: c.fail(LLMUnavailable("overloaded")), {}, "model_unavailable"),
    ],
    ids=["nothing-to-translate", "no-language", "incomplete", "refused", "truncated", "bad-shape", "unavailable"],
)
def test_a_translation_the_model_did_not_deliver_fails_the_step(
    claude: FakeClaude, script: Any, kw: dict[str, Any], rule: str
) -> None:
    script(claude)
    err = _failure(**({"context": {"copywrite.v3": COPY}} | kw))
    assert err.rule == rule and "MODEL-PROSE" not in str(err)


def test_a_slow_translation_fails_inside_the_step_deadline(claude: FakeClaude, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.execution_svc import STEP_TIMEOUT_SECONDS

    assert translate.BUDGET_SECONDS < STEP_TIMEOUT_SECONDS
    monkeypatch.setattr(translate, "BUDGET_SECONDS", 0.05)
    claude.cut_stream("", purpose="worker.translate.42")  # never answers
    step = WORKER.run(INTENT, RATIONALE, context={"copywrite.v3": COPY})
    with pytest.raises(ModelStepError) as info:  # a TimeoutError here means no budget of its own
        asyncio.run(asyncio.wait_for(step, timeout=2))
    assert info.value.rule == "model_truncated"


def test_secrets_in_upstream_text_never_reach_the_prompt(claude: FakeClaude) -> None:
    leaky = {"hero": {"headline": "Key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV here", "subtitle": "s"}, "sections": []}
    claude.reply(_draft(_lang("tl", "Tagalog", {"S1": "a", "S2": "b"})))
    run(context={"copywrite.v3": leaky})
    assert "ABCDEFGHIJKLMNOPQRSTUV" not in claude.calls[0].user


def test_off_claude_the_step_is_not_attempted(fake_claude: FakeClaude) -> None:
    assert _failure(context={"copywrite.v3": COPY}).rule == "model_not_configured"
    assert fake_claude.calls == []
    assert WORKER.step_model(None, None) is None


def test_the_registry_serves_the_real_worker() -> None:
    assert isinstance(WORKERS["agt_10b6"], translate.Translate42)
