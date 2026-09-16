"""Reputation-floor visibility on the FREE-FORM planning path (story 3.02).

The curated demo-kit path has reported floor actions since story 3.01, but the
model path — every non-curated intent, i.e. the normal product — built its
`DecomposeResponse` with no notices at all: `_registry_prompt_fragment`
computed the routable set, threw the complement away, and logged the starvation
relaxation to a server log no buyer will ever read. Demo a tetris intent and
the feature looked finished.

The first test here is a PIN, not a feature test. Surfacing the discarded
complement means changing how the routable set is computed, and the planning
prompt is the one string in this service whose exact bytes decide what the LLM
plans. If it shifts by a character the plans shift with it and every downstream
assertion drifts for a reason no failure message would name. So the block is
pinned literally, before the refactor, in both of its shapes: the ordinary
floor-filtered listing and the starvation backstop's top-N.
"""

from __future__ import annotations

import pytest

from app.seed import seed_registry
from app.services import orchestrator_svc
from app.services.reputation_svc import RepInfo
from app.state import state


def _info(agent_id: str, *, smoothed: int, lower: int) -> RepInfo:
    return RepInfo(
        agent_id=agent_id,
        smoothed_bps=smoothed,
        lower_bound_bps=lower,
        avg_bps=smoothed,
        count=3,
        weight=5 * 10_000_000,
        disputed=0,
        dispute_rate_bps=0,
        source="onchain",
    )


@pytest.fixture()
def seeded() -> object:
    """Fresh 12-agent registry, restored after — every seeded agent has a local
    worker, so the whole catalog is dispatchable and the prompt block below is
    a function of the reputation snapshot alone."""
    saved = dict(state.agents)
    state.agents.clear()
    seed_registry()
    yield
    state.agents.clear()
    state.agents.update(saved)


# Rep snapshot for the pin: every agent rated, scores fanned out so ordering is
# observable, with two deliberate holes —
#   * agt_04m1's lower bound is under the 5500 floor, so it is filtered out;
#   * agt_06q4 has no entry at all, so its line must fall back to the registry's
#     seeded `rep` (4.71) rather than a smoothed score.
def _pinned_reps() -> dict[str, RepInfo]:
    reps = {a.id: _info(a.id, smoothed=6000 + i * 100, lower=6000) for i, a in enumerate(state.list_agents())}
    reps["agt_04m1"] = _info("agt_04m1", smoothed=6300, lower=100)
    del reps["agt_06q4"]
    return reps


# Byte-for-byte, including the `name="…"` quoting the prompt-injection fence
# adds, the 3-decimal price, the 2-decimal rep on the 0–5 scale, and the
# comma-joined skills. Written out rather than rebuilt from a format string: a
# pin that recomputes the thing it pins cannot catch the thing it is for.
PINNED_BLOCK = """AVAILABLE_AGENTS:
- id=agt_01h8 name="copywrite.v3" price=0.012 rep=3.00 skills=copy,seo,en
- id=agt_02k2 name="design.figma" price=0.018 rep=3.05 skills=ui,tokens,figma
- id=agt_03d9 name="code.next" price=0.066 rep=3.10 skills=ts,react,next
- id=agt_05x7 name="seo.brief" price=0.009 rep=3.20 skills=seo,research
- id=agt_06q4 name="vision.ocr" price=0.014 rep=4.71 skills=vision,ocr
- id=agt_07w3 name="ads.meta" price=0.022 rep=3.30 skills=ads,meta
- id=agt_08j2 name="deploy.v0" price=0.011 rep=3.35 skills=deploy,ci,seal
- id=agt_09l5 name="research.pro" price=0.024 rep=3.40 skills=research,citations
- id=agt_10b6 name="translate.42" price=0.007 rep=3.45 skills=i18n,42 langs
- id=agt_11c0 name="code.gen" price=0.054 rep=3.50 skills=code,html,js,build
- id=agt_12r0 name="code.critic" price=0.052 rep=3.55 skills=a11y,polish,review"""

# The starvation backstop's shape: nobody clears the floor, so the block is the
# top _MIN_ROUTABLE_AGENTS by SMOOTHED score (not lower bound), best first —
# an ordering the planner reads as a ranking, so it is pinned too.
PINNED_STARVED_BLOCK = """AVAILABLE_AGENTS:
- id=agt_12r0 name="code.critic" price=0.052 rep=0.56 skills=a11y,polish,review
- id=agt_11c0 name="code.gen" price=0.054 rep=0.55 skills=code,html,js,build
- id=agt_10b6 name="translate.42" price=0.007 rep=0.55 skills=i18n,42 langs"""


def test_registry_prompt_block_is_byte_identical(seeded: object) -> None:
    assert orchestrator_svc._registry_prompt_fragment(_pinned_reps()) == PINNED_BLOCK


def test_starved_registry_prompt_block_is_byte_identical(seeded: object) -> None:
    reps = {a.id: _info(a.id, smoothed=1000 + i * 10, lower=100) for i, a in enumerate(state.list_agents())}

    assert orchestrator_svc._registry_prompt_fragment(reps) == PINNED_STARVED_BLOCK


def test_registry_prompt_block_is_stable_across_runs(seeded: object) -> None:
    # Nothing in the block may depend on set iteration, dict ordering or a
    # clock: the same snapshot must render the same bytes every time.
    reps = _pinned_reps()

    assert orchestrator_svc._registry_prompt_fragment(reps) == orchestrator_svc._registry_prompt_fragment(reps)
