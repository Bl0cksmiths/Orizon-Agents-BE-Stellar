"""deploy.v0 worker — seals the final artifact and synthesizes a preview URL.

Deterministic, no LLM. Reads the artifact the latest code step delivered —
code.gen, code.next, code.critic, or a bound operator's — from `context`
(`context.latest_output`), computes file count + total byte size, and returns a
believable "preview URL" string for the trace. The actual on-chain attestation
still happens in execution_svc._settle_onchain() — this worker only emits the
display-layer "sealed · preview ready" message that makes the pipeline feel
like a real CI/CD step.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
from typing import Any

from .base import Worker
from .context import CODE_ROLES, latest_output


def _slugify(s: str, max_len: int = 24) -> str:
    """Lowercase, ascii-only, dash-separated — for fake preview URLs."""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        s = "artifact"
    return s[:max_len].rstrip("-")


def _short_id(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]


def _sealable(context: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
    """The role and output of the build to seal: the last artifact delivered,
    which is also the one the run hands the buyer (`execution_svc` keeps the
    last artifact any step returned)."""
    return latest_output(context, CODE_ROLES, include_external=True)


class DeployV0(Worker):
    id = "agt_08j2"
    name = "deploy.v0"
    real = True

    def upstream_sources(self, context: dict[str, Any] | None) -> list[str]:
        sealed = _sealable(context)
        return [sealed[0]] if sealed else []

    async def run(
        self,
        intent: str,
        rationale: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # ~300ms of simulated "deploy" work so the trace feels alive.
        await asyncio.sleep(0.28 + random.random() * 0.18)

        ctx = context or {}
        sealed = _sealable(ctx)
        artifact = sealed[1].get("artifact") if sealed else None

        if not isinstance(artifact, dict):
            # No artifact to seal — emit a soft, honest summary so the
            # pipeline still completes for non-code intents.
            return {
                "summary": "no artifact to seal (non-code workflow)",
                "preview_url": None,
                "bytes": 0,
                "files": 0,
                "lines": 0,
                "counts": {"files": 0, "bytes": 0},
            }

        files = artifact.get("files", []) or []
        total_bytes = sum(len(f.get("content", "")) for f in files)
        total_lines = sum(f.get("content", "").count("\n") + 1 for f in files)
        title = artifact.get("title") or "artifact"

        kit = ctx.get("kit") or {}
        slug = _slugify(kit.get("kit_id") or title)
        sid = _short_id(f"{title}|{total_bytes}|{intent}")
        preview_url = f"https://{slug}.orizon.flow/preview/{sid}"

        size_kb = total_bytes / 1024
        summary = (
            f"sealed {title} · {len(files)} file{'s' if len(files) != 1 else ''} · "
            f"{total_lines:,} lines · {size_kb:.1f} KB · preview ready"
        )

        return {
            "summary": summary,
            "preview_url": preview_url,
            "bytes": total_bytes,
            "files": len(files),
            "lines": total_lines,
            "counts": {
                "files": len(files),
                "bytes": total_bytes,
                "lines": total_lines,
            },
        }
