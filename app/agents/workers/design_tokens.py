"""design.figma worker — locks design tokens (palette + typography + motion).

When the pipeline is running a curated demo kit, this worker returns the
kit's hand-tuned palette + typography deterministically (no LLM call, ~0.3s
of simulated "evaluation" so the trace feels alive).

For free-form intents (no kit), it calls the worker LLM to invent a tasteful
token set tailored to the intent.

Output dict shape (consumed by code.gen and emitted in the trace):
    {
        "summary": "palette · typography · accent · grid",
        "palette": { bg, surface, surface_2, border, text, muted, primary, accent, danger },
        "typography": { family_ui, family_display, base_size_px, scale },
        "css_vars": ":root { ... }",
        "counts": { "tokens": int },
    }
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from pydantic import BaseModel, Field

from ...config import settings
from ..model_factory import lazy_agent
from .base import Worker
from .prompt_safety import worker_prompt


class _TokensOutput(BaseModel):
    bg: str = Field(..., description="App background, e.g. #0B0414")
    surface: str
    surface_2: str
    border: str
    text: str
    muted: str
    primary: str
    accent: str
    danger: str
    family_ui: str = Field(..., description="Body font family stack, e.g. 'Inter, system-ui, sans-serif'")
    family_display: str = Field(..., description="Display/headline font family stack")


_INSTRUCTIONS = (
    "You are a senior product designer at a top studio. Given a build intent, "
    "return a tasteful, on-brand set of design tokens for a single-file HTML "
    "app: 9 hex colors (bg, surface, surface_2, border, text, muted, primary, "
    "accent, danger) and 2 font-family stacks (family_ui for body, family_display "
    "for headlines). Prefer dark UI unless the intent screams 'light'. Pick "
    "colors that have WCAG AA contrast. Use only web-safe / system font names "
    "(no Google Fonts CDN). Be concise — return only the token JSON."
)


class DesignTokens(Worker):
    id = "agt_02k2"
    name = "design.figma"
    real = True

    def __init__(self) -> None:
        self._agent = lazy_agent(
            name="design.figma",
            model_id=settings.worker_model,
            instructions=_INSTRUCTIONS,
            output_schema=_TokensOutput,
        )

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kit = (context or {}).get("kit")

        # ── Kit fast path: deterministic, no LLM ───────────────────────────
        if kit:
            palette = kit["palette"]
            typo = kit["typography"]
            # A tiny pause so the agent feels like it's doing real work in the trace.
            await asyncio.sleep(0.25 + random.random() * 0.15)
            return self._pack(
                palette=palette,
                typography=typo,
                source=f"kit:{kit['kit_id']}",
            )

        # ── Free-form path: LLM-generated tokens ───────────────────────────
        prompt = worker_prompt(intent, rationale, "Return the design tokens.")
        result = await self._agent.arun(prompt)
        out: _TokensOutput = result.content
        palette = {
            "bg": out.bg,
            "surface": out.surface,
            "surface_2": out.surface_2,
            "border": out.border,
            "text": out.text,
            "muted": out.muted,
            "primary": out.primary,
            "accent": out.accent,
            "danger": out.danger,
        }
        typography = {
            "family_ui": out.family_ui,
            "family_display": out.family_display,
            "base_size_px": 16,
            "scale": 1.25,
        }
        return self._pack(palette=palette, typography=typography, source="llm")

    @staticmethod
    def _css_vars(palette: dict[str, Any]) -> str:
        keys = [
            "bg",
            "surface",
            "surface_2",
            "border",
            "text",
            "muted",
            "primary",
            "accent",
            "danger",
        ]
        body = "\n".join(f"  --{k.replace('_', '-')}: {palette[k]};" for k in keys)
        return ":root {\n" + body + "\n}"

    @classmethod
    def _pack(
        cls,
        *,
        palette: dict[str, Any],
        typography: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        family_ui_short = typography["family_ui"].split(",")[0].strip().strip("'\"")
        summary = (
            f"palette {palette['primary']} / {palette['accent']} / {palette['danger']} · "
            f"{family_ui_short} · "
            f"surface {palette['surface']}"
        )
        return {
            "summary": summary,
            "palette": palette,
            "typography": typography,
            "css_vars": cls._css_vars(palette),
            "source": source,
            "counts": {"tokens": len(palette) + len(typography)},
        }
