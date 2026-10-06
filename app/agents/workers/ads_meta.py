"""ads.meta — a Meta (Facebook / Instagram) ad set's copy, audience notes and CTA.

Built on whatever the pipeline wrote before it (`context.CONSUMES["ads.meta"]`:
the page copy, the SEO brief, the research, the design tone, a translation),
handed on fenced. The copy follows the same facts rule as copywrite.v3: no
guarantee, price, statistic, testimonial, count or award the request did not
state — a clearly marked placeholder stands in for one.

Lengths follow Meta's recommended character counts for feed placements, past
which the text is cut off behind "See more" or an ellipsis: primary text 125,
headline 40, description 30. Structured outputs cannot enforce lengths, so the
model drafts unbounded (`AdSetDraft`) and `fit_ad_set` fits in code — at a
sentence or clause boundary only, never mid-sentence; a variant that cannot be
fitted that way is dropped and listed in `issues`. The CTA
is one of Meta's call-to-action button types, the objective one of its six
campaign objectives (the API's `OUTCOME_*` values). A special ad category
(credit, employment, housing, social issues) gets Meta's restricted targeting:
no age narrowing, which `fit_ad_set` enforces.

Any figure in the copy that is in neither the request nor the upstream outputs
is listed in `unverified_figures` for the owner to check before publishing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from . import claude_step
from .bounds import at_most, clamp, trim_items, trim_text
from .claude_only import ClaudeOnlyWorker
from .prompt_safety import worker_prompt

if TYPE_CHECKING:
    from ...llm.tiers import Tier

PRIMARY_TEXT_MAX = 125
HEADLINE_MAX = 40
DESCRIPTION_MAX = 30
MIN_ADS, MAX_ADS = 2, 5
MAX_INTERESTS = 10
MAX_LOCATIONS = 10
MAX_NOTES = 6
MAX_NOTE_CHARS = 240
MIN_AGE, MAX_AGE = 18, 65  # Meta's age range; 65 means "65+"

Cta = Literal[
    "LEARN_MORE",
    "SHOP_NOW",
    "SIGN_UP",
    "BOOK_NOW",
    "CONTACT_US",
    "DOWNLOAD",
    "GET_OFFER",
    "GET_QUOTE",
    "SUBSCRIBE",
    "APPLY_NOW",
    "ORDER_NOW",
    "DONATE_NOW",
    "SEND_MESSAGE",
    "WATCH_MORE",
    "GET_DIRECTIONS",
]
Objective = Literal[
    "OUTCOME_AWARENESS",
    "OUTCOME_TRAFFIC",
    "OUTCOME_ENGAGEMENT",
    "OUTCOME_LEADS",
    "OUTCOME_APP_PROMOTION",
    "OUTCOME_SALES",
]
SpecialAdCategory = Literal["NONE", "CREDIT", "EMPLOYMENT", "HOUSING", "ISSUES_ELECTIONS_POLITICS"]


class AdDraft(BaseModel):
    primary_text: str = Field(..., description="Main ad text, at most 125 characters.")
    headline: str = Field(..., description="At most 40 characters.")
    description: str = Field(..., description="At most 30 characters, or empty.")
    cta: Cta


class AudienceDraft(BaseModel):
    summary: str = Field(..., description="One or two sentences on who the ads are for.")
    locations: list[str] = Field(..., description="Countries, regions or cities the request names or implies.")
    age_min: int = Field(..., description="18 to 65.")
    age_max: int = Field(..., description="18 to 65; 65 means 65 and over.")
    interests: list[str] = Field(..., description="Up to 10 Meta interest-targeting ideas.")
    exclusions: list[str] = Field(..., description="Audiences to exclude, or empty.")


class AdSetDraft(BaseModel):
    """What Claude is asked for, unbounded; `fit_ad_set` applies the limits."""

    objective: Objective
    special_ad_category: SpecialAdCategory
    ads: list[AdDraft] = Field(..., description="3 to 5 distinct ad variants.")
    audience: AudienceDraft
    notes: list[str] = Field(..., description="Placeholders to fill and anything the owner must check, or empty.")


INSTRUCTIONS = (
    "You are a performance marketer who writes Meta (Facebook and Instagram) "
    "ad sets. Given a request, return one ad set: the campaign objective, the "
    "special ad category (NONE unless the ads offer credit, employment or "
    "housing, or are about social issues, elections or politics), 3 to 5 "
    "distinct ad variants, and audience notes.\n\n"
    "Each variant has primary text of at most 125 characters (hook first), a "
    "headline of at most 40 characters, a description of at most 30 characters, "
    "and the call-to-action button that fits the objective. Variants should "
    "test different angles, not reword one line. Follow Meta's advertising "
    "standards: no personal attributes ('Are you overweight?'), no "
    "before-and-after claims, no sensational or misleading wording.\n\n"
    "Audience notes: who to reach, the locations the request names or clearly "
    "implies, an age range, up to 10 interest-targeting ideas and any "
    "exclusions. For a special ad category leave the age range at 18 to 65.\n\n"
    "Facts: never invent facts the request does not state. That means no "
    "guarantees or refund promises, no prices or discounts, no statistics or "
    "results, no testimonials or quotes, no customer counts or member numbers, "
    "and no awards, certifications or press mentions. Where the copy needs one, "
    "write a clearly marked placeholder for the owner to fill in, such as "
    "[placeholder: discount] or [placeholder: price], and list it in the notes. "
    "Do not embellish either: no qualifiers (expert, certified, award-winning), "
    "times, places or extras the request and the upstream outputs do not give, "
    "and no implied comparisons or savings ('stop paying for…', 'cheaper than a "
    "shop', 'save money') the inputs do not state."
)

UPSTREAM_GUIDANCE = (
    "Build the ads on them: reuse the page copy's promise and tone, the SEO "
    "brief's brand name and audiences, and the design's tone of voice; where a "
    "translation is present, write in its language. Research findings are "
    "background, not facts the buyer stated — never turn them into statistics, "
    "guarantees or claims the request did not make."
)

# Room for the JSON plus any thinking the tier's model does first.
MAX_TOKENS = 8_000

_FIGURE_RE = re.compile(r"\d[\d,.]*")
_PLACEHOLDER_RE = re.compile(r"\[placeholder:[^\]]*\]", re.IGNORECASE)


def cta_label(cta: str) -> str:
    """Meta's button text for a CTA type: SHOP_NOW → "Shop now"."""
    return cta.replace("_", " ").capitalize()


# A boundary further back than this share of the limit leaves too little copy:
# the variant is dropped rather than shipped as a stub.
MIN_KEPT_SHARE = 0.4
_SENTENCE_ENDS = ".!?"
_CLAUSE_BREAKS = (", ", "; ", ": ", " — ", " – ", " - ")
_OPENERS, _CLOSERS = "([", ")]"


def fit_at_boundary(text: str, limit: int) -> str | None:
    """`text` (spacing collapsed) if it fits in `limit`; else the longest prefix
    that ends a sentence or a clause and fits, or None when no boundary leaves
    at least `MIN_KEPT_SHARE` of the limit. Never cuts inside brackets — a
    "[placeholder: …]" is kept whole or not at all — and never mid-sentence."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    floor = limit * MIN_KEPT_SHARE
    best: str | None = None
    depth = 0
    for i, ch in enumerate(text[: limit + 1]):
        if depth == 0 and i <= limit and any(text.startswith(sep, i) for sep in _CLAUSE_BREAKS):
            clause = text[:i].rstrip(" ,;:—–-")
            if len(clause) >= floor and (best is None or len(clause) > len(best)):
                best = clause
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS and depth:
            depth -= 1
        elif depth == 0 and ch in _SENTENCE_ENDS and i + 1 <= limit and (i + 1 == len(text) or text[i + 1] == " "):
            sentence = text[: i + 1]
            if len(sentence) >= floor and (best is None or len(sentence) > len(best)):
                best = sentence
    return best


def fit_ad_set(draft: AdSetDraft) -> dict[str, Any]:
    """The draft inside Meta's lengths and ranges.

    Over-long text is cut back to a sentence or clause boundary
    (`fit_at_boundary`), never mid-sentence; a variant with a field no boundary
    can fit is dropped and listed in `issues`, as is a duplicate. Fewer than
    two usable variants stays invalid."""
    ads: list[dict[str, str]] = []
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    limits = {"primary_text": PRIMARY_TEXT_MAX, "headline": HEADLINE_MAX, "description": DESCRIPTION_MAX}
    for index, ad in enumerate(draft.ads, 1):
        fitted = {field: fit_at_boundary(getattr(ad, field), limit) for field, limit in limits.items()}
        unfit = [field for field, value in fitted.items() if value is None]
        if unfit:
            issues.append({"variant": index, "problem": "dropped_no_boundary_fits", "fields": unfit})
            continue
        primary, headline = fitted["primary_text"] or "", fitted["headline"] or ""
        if not (primary and headline):
            issues.append({"variant": index, "problem": "dropped_empty"})
            continue
        key = f"{primary.casefold()}|{headline.casefold()}"
        if key in seen:
            issues.append({"variant": index, "problem": "dropped_duplicate"})
            continue
        seen.add(key)
        ads.append(
            {
                "primary_text": primary,
                "headline": headline,
                "description": fitted["description"] or "",
                "cta": ad.cta,
                "cta_label": cta_label(ad.cta),
            }
        )
    if len(ads) < MIN_ADS:
        raise claude_step.ModelStepError(
            claude_step.INVALID_OUTPUT, f"ads.meta: {len(ads)} usable ad variants, needs {MIN_ADS}"
        )
    audience = draft.audience
    restricted = draft.special_ad_category != "NONE"
    low = int(clamp(audience.age_min, MIN_AGE, MAX_AGE))
    high = int(clamp(audience.age_max, MIN_AGE, MAX_AGE))
    if restricted or low > high:
        low, high = MIN_AGE, MAX_AGE
    return {
        "objective": draft.objective,
        "special_ad_category": draft.special_ad_category,
        "ads": at_most(ads, MAX_ADS),
        "audience": {
            "summary": trim_text(audience.summary, 300),
            "locations": at_most(trim_items(audience.locations, 80), MAX_LOCATIONS),
            "age_min": low,
            "age_max": high,
            "interests": at_most(trim_items(audience.interests, 60), MAX_INTERESTS),
            "exclusions": at_most(trim_items(audience.exclusions, 120), MAX_INTERESTS),
        },
        "notes": at_most(trim_items(draft.notes, MAX_NOTE_CHARS), MAX_NOTES),
        "issues": issues,
    }


def unverified_figures(ads: list[dict[str, str]], sources: str) -> list[str]:
    """Figures in the ad copy that appear in none of `sources` (the request and
    the upstream outputs) — for the owner to check before the ads go live."""
    known = {m.group(0).rstrip(".,") for m in _FIGURE_RE.finditer(sources)}
    found: list[str] = []
    for ad in ads:
        for field in ("primary_text", "headline", "description"):
            text = _PLACEHOLDER_RE.sub(" ", ad[field])
            for match in _FIGURE_RE.finditer(text):
                figure = match.group(0).rstrip(".,")
                if figure and figure not in known and figure not in found:
                    found.append(figure)
    return found


class AdsMeta(ClaudeOnlyWorker):
    id = "agt_07w3"
    name = "ads.meta"
    real = True
    default_tier = "low"  # short, well-shaped copy
    reads_upstream = True

    def build_prompt(self, intent: str, rationale: str, context: dict[str, Any] | None = None) -> str:
        """The ad-set prompt: the fenced request, then the fenced upstream outputs."""
        return worker_prompt(
            intent,
            rationale,
            "Return the Meta ad set.",
            sections=[self.handoff(context).section(UPSTREAM_GUIDANCE)],
        )

    async def run_on_claude(self, intent: str, rationale: str, context: dict[str, Any], tier: Tier) -> dict[str, Any]:
        draft = await claude_step.structured(
            worker=self.name,
            tier=tier,
            system=INSTRUCTIONS,
            user=self.build_prompt(intent, rationale, context),
            schema=AdSetDraft,
            max_tokens=MAX_TOKENS,
        )
        ad_set = fit_ad_set(draft)
        sources = "\n".join([intent, rationale, self.handoff(context).text()])
        figures = unverified_figures(ad_set["ads"], sources)
        first = ad_set["ads"][0]
        return {
            "summary": f"{len(ad_set['ads'])} Meta ad variants — {first['headline']}",
            **ad_set,
            "unverified_figures": figures,
            "counts": {"ads": len(ad_set["ads"]), "interests": len(ad_set["audience"]["interests"])},
        }
