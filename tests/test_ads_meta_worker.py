"""ads.meta on Claude, on FakeClaude only: Meta's lengths and ranges fitted in
code, the upstream copy fenced into the prompt, the facts rule asked for and
unbacked figures surfaced, every model failure classed, and no stand-in output
off Claude."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.registry import WORKERS
from app.agents.workers import ads_meta
from app.agents.workers.claude_step import ModelStepError
from app.config import settings
from app.llm.errors import LLMUnavailable
from app.llm.testing import FakeClaude

INTENT = "Meta ads for our neighbourhood bike repair co-op in Quezon City, open Saturdays"
RATIONALE = "turn the landing copy into an ad set"
WORKER = ads_meta.AdsMeta()


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def _ad(headline: str = "Fix your bike, together", **kw: Any) -> dict[str, Any]:
    return {
        "primary_text": kw.get("primary_text", "Bring your bike on Saturday. Our stands and tools are yours to use."),
        "headline": headline,
        "description": kw.get("description", "Open Saturdays"),
        "cta": kw.get("cta", "LEARN_MORE"),
    }


def _draft(*ads: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {
        "objective": kw.get("objective", "OUTCOME_TRAFFIC"),
        "special_ad_category": kw.get("special_ad_category", "NONE"),
        "ads": list(ads) or [_ad(), _ad("Learn to fix a flat"), _ad("Your co-op workshop")],
        "audience": {
            "summary": "Commuters and weekend riders nearby.",
            "locations": ["Quezon City"],
            "age_min": kw.get("age_min", 18),
            "age_max": kw.get("age_max", 45),
            "interests": ["Cycling", "Bicycle commuting"],
            "exclusions": [],
        },
        "notes": ["Fill in [placeholder: workshop address]."],
    }


def run(context: dict[str, Any] | None = None, tier: Any = None, intent: str = INTENT) -> dict[str, Any]:
    return asyncio.run(WORKER.run(intent, RATIONALE, context=context, tier=tier))


def _failure(**kw: Any) -> ModelStepError:
    with pytest.raises(ModelStepError) as info:
        run(**kw)
    return info.value


def test_an_ad_set_is_written_on_haiku_and_handed_on(claude: FakeClaude) -> None:
    claude.reply(_draft())
    out = run()

    (call,) = claude.calls
    assert (call.purpose, call.model, call.effort) == ("worker.ads.meta", "claude-haiku-4-5", None)
    assert call.schema_name == "AdSetDraft" and call.system == ads_meta.INSTRUCTIONS
    assert call.max_tokens == ads_meta.MAX_TOKENS
    assert out["summary"] == "3 Meta ad variants — Fix your bike, together"
    assert out["objective"] == "OUTCOME_TRAFFIC" and out["special_ad_category"] == "NONE"
    assert out["ads"][0] == {
        "primary_text": "Bring your bike on Saturday. Our stands and tools are yours to use.",
        "headline": "Fix your bike, together",
        "description": "Open Saturdays",
        "cta": "LEARN_MORE",
        "cta_label": "Learn more",
    }
    assert out["audience"]["locations"] == ["Quezon City"] and (
        out["audience"]["age_min"],
        out["audience"]["age_max"],
    ) == (18, 45)
    assert out["counts"] == {"ads": 3, "interests": 2}
    assert out["unverified_figures"] == []


def test_the_steps_tier_picks_the_model(claude: FakeClaude) -> None:
    claude.reply(_draft())
    run(tier="moderate")
    assert (claude.calls[0].model, claude.calls[0].effort) == ("claude-sonnet-5-5", "medium")


def test_the_copy_is_fitted_to_metas_lengths(claude: FakeClaude) -> None:
    long = _ad(
        "A headline that runs well past the forty characters Meta shows",
        primary_text="Bring your bike. " * 20,
        description="A description far past thirty characters",
    )
    claude.reply(_draft(long, _ad("Second"), _ad("Third"), _ad("Fourth"), _ad("Fifth"), _ad("Sixth")))
    out = run()
    ad = out["ads"][0]
    assert len(ad["primary_text"]) <= ads_meta.PRIMARY_TEXT_MAX
    assert len(ad["headline"]) <= ads_meta.HEADLINE_MAX
    assert len(ad["description"]) <= ads_meta.DESCRIPTION_MAX
    assert len(out["ads"]) == ads_meta.MAX_ADS


def test_ages_are_held_to_metas_range_and_a_special_category_is_not_narrowed(claude: FakeClaude) -> None:
    claude.reply(_draft(age_min=13, age_max=90))
    audience = run()["audience"]
    assert (audience["age_min"], audience["age_max"]) == (18, 65)
    claude.reply(_draft(special_ad_category="EMPLOYMENT", age_min=25, age_max=34))
    restricted = run()
    assert restricted["special_ad_category"] == "EMPLOYMENT"
    assert (restricted["audience"]["age_min"], restricted["audience"]["age_max"]) == (18, 65)


def test_the_upstream_copy_is_fenced_into_the_prompt_after_the_request(claude: FakeClaude) -> None:
    copy = {"hero": {"headline": "UPSTREAM-HEADLINE Fix it together"}, "sections": [{"title": "Tools", "body": "b"}]}
    claude.reply(_draft())
    run(context={"copywrite.v3": copy})
    user = claude.calls[0].user
    assert user.index("END USER_INPUT") < user.index("BEGIN UPSTREAM_OUTPUTS") < user.index("UPSTREAM-HEADLINE")
    assert user.index("UPSTREAM-HEADLINE") < user.index("END UPSTREAM_OUTPUTS")
    assert user.rstrip().endswith("Return the Meta ad set.")
    assert WORKER.upstream_sources({"copywrite.v3": copy}) == ["copywrite.v3"]


def test_the_facts_rule_is_asked_for_and_unbacked_figures_are_surfaced(claude: FakeClaude) -> None:
    assert "never invent facts" in ads_meta.INSTRUCTIONS
    claude.reply(
        _draft(
            _ad("Join 500+ riders", primary_text="Save 20% on repairs. [placeholder: 2 hours] free stand time."),
            _ad("Open Saturdays from 9", primary_text="Tools for everyone."),
        )
    )
    out = run(intent=INTENT + " from 9 to 5")
    assert out["unverified_figures"] == ["20", "500"]  # "9" is in the request; the placeholder's "2" is not copy


@pytest.mark.parametrize(
    ("script", "rule"),
    [
        (lambda c: c.refuse(category="cyber", explanation="MODEL-PROSE"), "model_refused"),
        (lambda c: c.truncate(partial='{"objective": "OUTCOME_'), "model_truncated"),
        (lambda c: c.reply(_draft(_ad("Only one"))), "invalid_output"),
        (lambda c: c.reply(_draft(_ad(), _ad())), "invalid_output"),  # duplicates are one variant
        (lambda c: c.reply(_draft(_ad(cta="CLICK_HERE"), _ad("b"))), "invalid_output"),
        (lambda c: c.fail(LLMUnavailable("rate_limited")), "model_unavailable"),
    ],
    ids=["refused", "truncated", "one-ad", "duplicates", "bad-cta", "unavailable"],
)
def test_an_ad_set_the_model_did_not_deliver_fails_the_step(claude: FakeClaude, script: Any, rule: str) -> None:
    script(claude)
    err = _failure()
    assert err.rule == rule and "MODEL-PROSE" not in str(err)


def test_off_claude_the_step_is_not_attempted(fake_claude: FakeClaude) -> None:
    assert _failure().rule == "model_not_configured"
    assert fake_claude.calls == []
    assert WORKER.step_model(None, None) is None
    assert WORKER.upstream_sources({"copywrite.v3": {"hero": {"headline": "h"}}}) == []


def test_the_registry_serves_the_real_worker() -> None:
    assert isinstance(WORKERS["agt_07w3"], ads_meta.AdsMeta)
