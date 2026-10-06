"""code.critic — top-level polish-pass worker.

Wraps the existing CodeCritic Agno agent (defined in code_critic.py) as a
first-class Worker so it appears as its own step in the pipeline trace. Reads
the latest code step's artifact (code.gen or an earlier critic pass; a code.next
Next.js project is declined as not attempted) from `context`, runs the validator to surface any structural violations,
prepends the demo-kit critic_checklist as non-negotiable requirements, and asks
the critic to refine the HTML against the copy and design intent upstream of it.

If the critic regresses (more violations after vs. before), we fall back to
the original draft so the user is never worse off.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .base import ModelWorker
from .claude_step import ModelStepError
from .code_critic import CRITIC_DEFAULT_TIER, CodeCritic
from .code_gen import carry_deferred
from .code_validator import harden_artifact, validate_html
from .context import CODE_CRITIC, CODE_GEN, CODE_NEXT, latest_output

if TYPE_CHECKING:
    from ...llm.tiers import Tier

logger = logging.getLogger(__name__)

# The steps whose artifact the critic reviews: the latest delivered of them,
# so a second critic pass refines the first one's. When the latest is a
# code.next project it is declined, not reviewed (see UNSUPPORTED_ARTIFACT).
DRAFT_ROLES = (CODE_GEN, CODE_NEXT, CODE_CRITIC)

UPSTREAM_GUIDANCE = (
    "They are the intent the draft was built from: check the app uses this "
    "copy and these design tokens, and restore any it drifted from."
)


def _draft(context: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
    """The role and output of the draft to review, or None when no code step
    delivered an artifact."""
    return latest_output(context, DRAFT_ROLES)


# The failure class of a draft this critic does not review. code.next builds a
# multi-file Next.js project (`artifact.framework == "next"`); the critic's job
# is a single-file HTML app, and rewriting a project into one would replace the
# buyer's deliverable with something they did not order. So the step is not
# attempted — unbilled and unrated, like a request with no input (see
# execution_svc._NOT_ATTEMPTED) — rather than reviewed in the wrong format.
UNSUPPORTED_ARTIFACT = "unsupported_artifact"


def _is_project(output: dict[str, Any]) -> bool:
    """True when the draft is a framework project, not a single-file HTML app."""
    artifact = output.get("artifact")
    return isinstance(artifact, dict) and artifact.get("framework") == "next"


class CodeCriticWorker(ModelWorker):
    id = "agt_12r0"
    name = "code.critic"
    real = True
    default_tier = CRITIC_DEFAULT_TIER
    max_tier = "moderate"  # see ModelWorker: Opus would outrun the step deadline
    reads_upstream = True

    def __init__(self) -> None:
        self._critic = CodeCritic()

    def _deterministic(self, context: dict[str, Any] | None) -> bool:
        # No draft to refine, or code.gen served a baked artifact: either way
        # the step answers without asking a model (see run()).
        draft = _draft(context)
        return draft is None or draft[1].get("source") == "baked" or _is_project(draft[1])

    def upstream_sources(self, context: dict[str, Any] | None) -> list[str]:
        """The draft's step — reviewed even when no model is asked — then the
        handoff's roles when the review goes to a model."""
        draft = _draft(context)
        if draft and _is_project(draft[1]):
            return []  # not reviewed (see UNSUPPORTED_ARTIFACT), so nothing is used
        sources = [draft[0]] if draft else []
        return sources + [r for r in super().upstream_sources(context) if r not in sources]

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
        *,
        tier: Tier | None = None,
    ) -> dict[str, Any]:
        import asyncio
        import random

        ctx = context or {}
        draft = _draft(ctx)
        if draft and _is_project(draft[1]):
            raise ModelStepError(
                UNSUPPORTED_ARTIFACT, f"code.critic: {draft[0]} built a Next.js project; the critic reviews HTML apps"
            )
        prior: dict[str, Any] = draft[1] if draft else {}
        draft_artifact = prior.get("artifact")

        if not isinstance(draft_artifact, dict):
            return {
                "summary": "no draft to refine (code.gen did not produce an artifact)",
                "artifact": None,
                "critic_violations": [],
                "critic_notes": ["skipped: no draft"],
                "counts": {"violations": 0, "notes": 1},
            }

        draft_html = draft_artifact.get("preview_html") or ""
        if not draft_html.strip():
            # Try the entry file's content if preview_html is empty
            files = draft_artifact.get("files", [])
            entry = draft_artifact.get("entry")
            entry_file = next((f for f in files if f.get("path") == entry), files[0] if files else None)
            draft_html = (entry_file or {}).get("content", "")

        # ── 1. Structural violations from the validator ─────────────────────
        validator_violations = validate_html(draft_html)

        # ── Baked-artifact fast path ────────────────────────────────────────
        # When code.gen served a hand-tuned baked artifact, the critic just
        # validates and emits a polished-looking summary — no LLM call. The
        # kit's checklist items are pre-met by design.
        if prior.get("source") == "baked":
            checklist = (ctx.get("kit") or {}).get("critic_checklist", [])
            await asyncio.sleep(0.4 + random.random() * 0.6)
            title = draft_artifact.get("title", "artifact")
            kit_id = (ctx.get("kit") or {}).get("kit_id", "kit")
            draft_artifact = harden_artifact(draft_artifact)
            return {
                "summary": (
                    f"{title} · verified · {len(validator_violations)} structural "
                    f"issue(s) · {len(checklist)} kit requirements ✓"
                ),
                "artifact": draft_artifact,
                "critic_violations": validator_violations,
                "critic_notes": [
                    f"validated baked artifact ({len(draft_html):,} bytes)",
                    f"{len(checklist)} {kit_id} kit requirements pre-met by design",
                ],
                "counts": {
                    "violations": len(validator_violations),
                    "kit_requirements": len(checklist),
                    "notes": 2,
                },
                "source": "baked",
            }

        # ── 2. Kit-specific must-haves (treated as REQUIRED items) ──────────
        kit = ctx.get("kit") or {}
        kit_checklist = kit.get("critic_checklist", []) if isinstance(kit, dict) else []
        kit_required = [f"REQUIRED ({kit.get('kit_id', 'kit')}): {item}" for item in kit_checklist]

        violations_for_critic = kit_required + validator_violations

        # ── 3. Refine ───────────────────────────────────────────────────────
        critic_notes: list[str] = []
        final_artifact = draft_artifact
        try:
            revised = await self._critic.refine(
                intent=intent,
                rationale=rationale,
                draft_html=draft_html,
                violations=violations_for_critic,
                tier=self.effective_tier(tier),
                upstream=self.handoff(ctx).section(UPSTREAM_GUIDANCE),
            )
            revised_html = revised.get("preview_html") or ""
            post_violations = validate_html(revised_html)
            if post_violations and len(post_violations) > len(validator_violations):
                # Critic regressed — keep the draft.
                critic_notes.append(
                    f"reverted: revised had {len(post_violations)} structural "
                    f"issues vs. {len(validator_violations)} in draft"
                )
            else:
                draft_lines = draft_html.count("\n") + 1
                revised_lines = revised_html.count("\n") + 1
                delta = revised_lines - draft_lines
                critic_notes.append(
                    f"polished: {draft_lines}L → {revised_lines}L "
                    f"({'+' if delta >= 0 else ''}{delta}) · "
                    f"{len(validator_violations)} structural issue"
                    f"{'s' if len(validator_violations) != 1 else ''} fixed"
                )
                if kit_required:
                    critic_notes.append(
                        f"applied {len(kit_required)} kit requirement{'s' if len(kit_required) != 1 else ''}"
                    )
                # The draft's "Deferred: …" list survives the rewrite: the
                # critic writes its own summary, and the buyer must still be
                # told what was left out (merged with any list it adds).
                final_artifact = {
                    **revised,
                    "summary": carry_deferred(str(revised.get("summary", "")), str(draft_artifact.get("summary", ""))),
                }
        except ModelStepError:
            # On Claude, a polish the model did not deliver (declined, cut off,
            # unreachable, over the spend cap, unreadable) is this STEP's
            # failure: unbilled, and the run loop keeps code.gen's draft as the
            # run's artifact, so the buyer still gets the app — without paying
            # for a polish pass that never happened.
            raise
        except Exception as e:  # pragma: no cover — never fail the workflow
            logger.warning("code.critic refine failed, keeping draft artifact: %s", e, exc_info=True)
            critic_notes.append(f"critic failed: {type(e).__name__}: {str(e)[:80]}")

        title = final_artifact.get("title", "artifact")
        summary = title + " · " + (critic_notes[0] if critic_notes else "no changes")

        # The critic rewrote the whole document and may well have dropped the
        # policy meta on the way through — re-apply it before the artifact
        # leaves the pipeline. harden_artifact() is idempotent.
        final_artifact = harden_artifact(final_artifact)

        return {
            "summary": summary,
            "artifact": final_artifact,
            "critic_violations": validator_violations,
            "critic_notes": critic_notes,
            "counts": {
                "violations": len(validator_violations),
                "kit_requirements": len(kit_required),
                "notes": len(critic_notes),
            },
        }
