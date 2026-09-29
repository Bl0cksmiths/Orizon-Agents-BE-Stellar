"""The verdict, and the checklist as terminal lines, Markdown and JSON.

The verdict is GO only when every REQUIRED check is PASS or WARN. A required
check that FAILED is NO-GO (exit 4); one that was SKIPPED is NO-GO too
(exit 5) — skipped means "not judged", and a take recorded on an unjudged
precondition is the wasted session this tool exists to prevent.

Every string that reaches a file passes through `redact.scrub`, like every
printed line.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .checks import FAIL, PASS, SKIPPED, WARN, Check, Facts
from .config import EXIT_GO, EXIT_INCOMPLETE, EXIT_NO_GO, JSON_NAME, MARKDOWN_NAME
from .redact import scrub

MARK = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", SKIPPED: "SKIP"}


@dataclass(frozen=True)
class RunFacts:
    api: str
    backend: str
    frontend: str
    rpc_url: str
    horizon_url: str
    generated_at: int  # unix seconds
    generated_utc: str


def verdict(checks: list[Check]) -> tuple[int, str]:
    """(exit code, one line). SKIPPED is never a pass."""
    failed = [c for c in checks if c.required and c.status == FAIL]
    skipped = [c for c in checks if c.required and c.status == SKIPPED]
    if failed:
        return EXIT_NO_GO, f"NO-GO — {len(failed)} required check(s) failed" + (
            f", {len(skipped)} skipped" if skipped else ""
        )
    if skipped:
        return EXIT_INCOMPLETE, f"NO-GO — {len(skipped)} required check(s) were skipped, and skipped is not a pass"
    return EXIT_GO, "GO — every required check passed"


def terminal_lines(check: Check) -> list[str]:
    tag = "" if check.required else " (advisory)"
    lines = [f"[{MARK[check.status]}] {check.id}{tag} — {check.title}", f"       {check.detail}"]
    if check.fix:
        lines.append(f"       fix: {check.fix}")
    return lines


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(checks: list[Check], facts: Facts, run: RunFacts, code: int, line: str) -> str:
    out = [
        "# Demo pre-flight (story 5.04)",
        "",
        f"**{line}** (exit {code})",
        "",
        f"Generated {run.generated_utc} against API `{run.api}`, backend `{run.backend}`, frontend `{run.frontend}`, "
        f"RPC `{run.rpc_url}`, Horizon `{run.horizon_url}`. Testnet only. Read-only"
        + (
            " except the one opted-in decompose."
            if any(c.id == "exclusion.decompose" and c.required for c in checks)
            else "."
        ),
        "",
    ]
    if facts.cold_start_seconds is not None:
        out += [f"Backend cold start: {facts.cold_start_seconds:.1f} s to the first 200 from /api/health.", ""]
    if facts.disclosures:
        out += ["## Disclose on camera", ""] + [f"- {d}" for d in facts.disclosures] + [""]
    out += [
        "## Checklist",
        "",
        "| Status | Check | Required | Finding | Fix |",
        "|---|---|---|---|---|",
    ]
    for c in checks:
        out.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (c.status, f"`{c.id}` {c.title}", "yes" if c.required else "advisory", c.detail, c.fix or "—")
            )
            + " |"
        )
    return scrub("\n".join(out) + "\n")


def render_json(checks: list[Check], facts: Facts, run: RunFacts, code: int, line: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "generated_at": run.generated_at,
        "network": "testnet",
        "verdict": "GO" if code == EXIT_GO else "NO-GO",
        "exit_code": code,
        "summary": line,
        "api": run.api,
        "backend": run.backend,
        "frontend": run.frontend,
        "cold_start_seconds": facts.cold_start_seconds,
        "disclosures": facts.disclosures,
        "checks": [asdict(c) for c in checks],
    }
    return json.loads(scrub(json.dumps(body)))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write(out_dir: Path, markdown: str, body: dict[str, Any]) -> tuple[Path, Path]:
    md_path = out_dir / MARKDOWN_NAME
    json_path = out_dir / JSON_NAME
    _atomic_write(md_path, markdown)
    _atomic_write(json_path, json.dumps(body, indent=2, sort_keys=True) + "\n")
    return md_path, json_path
