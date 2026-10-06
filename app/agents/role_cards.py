"""Role cards: what each built-in agent does, what it reads, and what it hands on.

The planner composes agents into a pipeline, and a pipeline is only as good as
its handoffs: each step reads what the steps before it produced
(`workers/context.py`, `CONSUMES`). A name and a skill list do not say that —
"copywrite.v3: copy, seo, en" does not tell the planner that the copy is what
design, code and ads build on, or that code.critic has nothing to do without a
draft before it. So every built-in agent carries a short card, rendered under
its line in AVAILABLE_AGENTS (`orchestrator_svc.render_agents_block`).

Rules for the text, because it is part of the planner's cached prompt prefix:

  * Static and first-party. A card never varies per request or per score, so
    the block stays byte-stable between registry changes and the prefix keeps
    caching. External agents get no card: their operator's text never enters
    the trusted half of the prompt beyond the sanitized name.
  * True to the worker. "Reads" is what the worker's handoff actually consumes
    and "hands on" is what it actually returns — written against the workers,
    not against a wish list. A card that promised an input the worker never
    reads would teach the planner a pipeline that does not exist.
  * One line, no `key=value` shapes, so it cannot be mistaken for an agent
    entry or a field of one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleCard:
    does: str
    reads: str
    hands_on: str
    # When to plan it — and, for the agents that need an input to exist, when not to.
    use_when: str

    def render(self) -> str:
        return f"does: {self.does}; reads: {self.reads}; hands on: {self.hands_on}; use when: {self.use_when}"


ROLE_CARDS: dict[str, RoleCard] = {
    "agt_09l5": RoleCard(
        does="researches the subject: findings with confidence, and sources",
        reads="the request, plus any text read from an image, audit findings or translations",
        hands_on="findings that seo.brief, copywrite.v3, design.figma, the code builders and ads.meta build on",
        use_when="the work needs facts, a feature brief, competitors, an audience or a report",
    ),
    "agt_05x7": RoleCard(
        does="writes the brand and search brief: name, tagline, keywords, audiences",
        reads="research.pro's findings, text read from an image, translations",
        hands_on="brand, tagline and keywords for copywrite.v3, design.figma and ads.meta",
        use_when="a site, page, product, campaign or content that people should find or recognise",
    ),
    "agt_01h8": RoleCard(
        does="writes the copy: headline, subtitle and titled sections",
        reads="seo.brief's brand and keywords, research.pro's findings, audit findings, text read from an image",
        hands_on="page copy that design.figma, the code builders, ads.meta and translate.42 use",
        use_when="anything with words a reader sees: pages, emails, posts, reports, plain-language summaries",
    ),
    "agt_02k2": RoleCard(
        does="sets the design tokens: colour palette and typography",
        reads="seo.brief's brand, copywrite.v3's copy, research.pro's findings",
        hands_on="tokens the code builders style the build with",
        use_when="before any build with a visual interface (site, app, game, tool)",
    ),
    "agt_11c0": RoleCard(
        does="builds a working single-file HTML, CSS and JavaScript app, site, game or tool",
        reads="copy, design tokens, research findings and the brand brief from earlier steps",
        hands_on="the code artifact that code.critic reviews and deploy.v0 seals",
        use_when="the buyer wants something built that runs in a browser",
    ),
    "agt_03d9": RoleCard(
        does="builds a React / Next.js app in TypeScript",
        reads="copy, design tokens, research findings and the brand brief from earlier steps",
        hands_on="a Next.js project that deploy.v0 seals (code.critic does not review it)",
        use_when="the buyer asks for React, Next.js or TypeScript specifically; never together with code.gen",
    ),
    "agt_12r0": RoleCard(
        does="reviews and fixes a code.gen build: accessibility, polish, edge cases",
        reads="code.gen's single-file HTML artifact, with the copy and design tokens it was built from",
        hands_on="the improved artifact, for deploy.v0 to seal",
        use_when="right after code.gen; never without it, and never for a code.next project",
    ),
    "agt_08j2": RoleCard(
        does="seals the finished build and issues a preview link",
        reads="the latest code artifact (after code.critic when it ran)",
        hands_on="the sealed build and its preview link, as the last step",
        use_when="a build should be live, shared or deployed; never without a code builder before it",
    ),
    "agt_04m1": RoleCard(
        does="audits a Solidity smart contract for security flaws, rated by severity",
        reads="the contract in the request, text read from an image, research.pro's findings",
        hands_on="findings that copywrite.v3 can explain in plain language or translate.42 can translate",
        use_when="a smart contract is to be audited, reviewed or secured",
    ),
    "agt_06q4": RoleCard(
        does="reads the text and structure out of images",
        reads="images uploaded with the request or linked in it by https URL",
        hands_on="the extracted text and its language for translate.42, copywrite.v3 or research.pro",
        use_when="the request includes an image or an https image link; never without one",
    ),
    "agt_07w3": RoleCard(
        does="writes a Meta (Facebook and Instagram) ad set: headlines, primary text, call to action",
        reads="copywrite.v3's copy, seo.brief's brand, research.pro's findings, design.figma's tone",
        hands_on="ad variants that translate.42 can localise",
        use_when="ads, a campaign or paid social promotion is asked for",
    ),
    "agt_10b6": RoleCard(
        does="translates earlier steps' text, or text quoted in the request, into the requested languages",
        reads="copy, ad variants, research findings, audit findings, text read from an image, the tagline",
        hands_on="the translations, to a later builder or as the deliverable",
        use_when="the output language is not English, several languages are asked for, or text is to be translated",
    ),
}


def card_for(agent_id: str) -> RoleCard | None:
    """The card for a built-in agent, or None (an external agent has none)."""
    return ROLE_CARDS.get(agent_id)
