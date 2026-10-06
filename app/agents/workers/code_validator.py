"""
Pure-Python validator + hardener for code.gen artifacts.

No LLM call — just fast regex/heuristic checks to catch obvious quality
issues before returning to the user. Feeds the critic with a precise list
of violations to fix.

Two jobs:

1. ``validate_html`` — structural quality checks, plus a scan of what the model
   actually put inside `<script>` for dangerous primitives. The generated HTML
   is rendered in the frontend, so "the model wrote 200 well-formed lines" was
   never enough on its own.
2. ``harden_artifact`` — the deterministic half. The scan is a heuristic and can
   be evaded; the injected Content-Security-Policy cannot, so the artifact is
   contained whether or not the scan spotted anything.
"""

from __future__ import annotations

import re
from typing import Any

_EXTERNAL_SCRIPT = re.compile(
    r'<script\b[^>]*\ssrc=["\'](?!data:|javascript:|#|/|\./|\.\./)([^"\']+)',
    re.IGNORECASE,
)
_EXTERNAL_STYLESHEET = re.compile(
    r'<link\b[^>]*\srel=["\']stylesheet["\'][^>]*\shref=["\'](?!data:|#|/|\./|\.\./)([^"\']+)',
    re.IGNORECASE,
)
_EXTERNAL_IMG = re.compile(
    r'<img\b[^>]*\ssrc=["\'](https?:)([^"\']+)',
    re.IGNORECASE,
)
_HAS_SCRIPT = re.compile(r"<script\b", re.IGNORECASE)
_HAS_HTML = re.compile(r"<html\b", re.IGNORECASE)
_HAS_HEAD = re.compile(r"<head\b", re.IGNORECASE)
_HAS_BODY = re.compile(r"<body\b", re.IGNORECASE)
_HAS_VIEWPORT = re.compile(
    r'<meta\b[^>]*\sname=["\']viewport["\']',
    re.IGNORECASE,
)

# ── Content-level scan of the generated JavaScript ─────────────────────────
#
# False positives are the real risk here: a landing page whose copy mentions
# "we never call fetch()", or a code-snippet app rendering `eval(` inside a
# <pre>, must still ship. So the scan never looks at the whole document — it
# only reads regions the browser will EXECUTE: <script> bodies, inline on*=
# handlers, and javascript: URLs. Prose and markup are ignored by construction.
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_INLINE_HANDLER = re.compile(r"""\son[a-z]{2,}\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_JS_URL = re.compile(r"""(?:href|src|action)\s*=\s*["']?\s*javascript:([^"'>]*)""", re.IGNORECASE)

# (regex, short human-readable label). Deliberately narrow: `localStorage` is
# NOT here — all four demo kits persist state with it, it is the documented
# house style in the code.gen prompt, and the preview iframe has no
# same-origin storage to read anyway. Only the primitives that could carry data
# out of the frame, or build code the scan cannot read, are listed.
_HAZARDS: list[tuple[re.Pattern[str], str]] = [
    # Network egress — the one channel the iframe sandbox does NOT close.
    (re.compile(r"\bfetch\s*\("), "fetch()"),
    (re.compile(r"\bXMLHttpRequest\b"), "XMLHttpRequest"),
    (re.compile(r"\bsendBeacon\s*\("), "navigator.sendBeacon()"),
    (re.compile(r"\bnew\s+WebSocket\b|\bWebSocket\s*\("), "WebSocket"),
    (re.compile(r"\bnew\s+EventSource\b|\bEventSource\s*\("), "EventSource"),
    (re.compile(r"(?<![.\w])import\s*\("), "dynamic import()"),
    (
        re.compile(r"""\b(?:window\.)?location(?:\.href)?\s*=\s*["']https?://"""),
        "navigation to a remote URL",
    ),
    (re.compile(r"""\blocation\.(?:replace|assign)\s*\(\s*["']https?://"""), "location.replace() to a remote URL"),
    # Dynamic evaluation — hides payloads from this scan and from review.
    (re.compile(r"\beval\s*\("), "eval()"),
    (re.compile(r"\bnew\s+Function\s*\("), "new Function()"),
    (re.compile(r"""\b(?:setTimeout|setInterval)\s*\(\s*["'`]"""), "setTimeout/setInterval with a code string"),
    # Reaching out of the frame / at credentials.
    (re.compile(r"\bwindow\.parent\b|\bparent\.postMessage\b"), "window.parent access"),
    (re.compile(r"\bwindow\.top\b|\btop\.location\b"), "window.top access"),
    (re.compile(r"\bdocument\.cookie\b"), "document.cookie access"),
]

# ── Deterministic containment ──────────────────────────────────────────────
#
# A CSP delivered as a <meta> tag inside the artifact travels with it: it applies
# in the preview iframe AND in the file the user downloads and opens from
# file://, where no sandbox attribute protects them.
#
# It is an allow-list, so it also covers egress tricks the regex scan above
# misses. `'unsafe-inline'` is required — the whole artifact contract is inline
# CSS/JS. `'unsafe-eval'` is deliberately ALLOWED: with `connect-src 'none'` and
# `form-action 'none'` there is nothing for evaluated code to exfiltrate to, so
# blocking it would only silently break a generated app that used `eval` for its
# own maths (a real risk — the model is told not to, but it is not a guarantee)
# without buying containment. `eval` is still reported as a violation so the
# critic rewrites it.
_CSP_MARKER = "orizon-artifact-csp"
_CSP_POLICY = (
    "default-src 'none'; "
    "script-src 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; "
    "media-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-src 'none'; "
    "object-src 'none'"
)
_CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{_CSP_POLICY}"><!-- {_CSP_MARKER} -->'

_HEAD_OPEN = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HTML_OPEN = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_SCRIPT_OPEN = re.compile(r"<script\b", re.IGNORECASE)


def _executable_regions(html: str) -> list[str]:
    """Everything in the document a browser will run as JavaScript."""
    regions = [m.group(1) for m in _SCRIPT_BLOCK.finditer(html)]
    regions.extend(m.group(1) for m in _INLINE_HANDLER.finditer(html))
    regions.extend(m.group(1) for m in _JS_URL.finditer(html))
    return regions


def scan_script_hazards(html: str) -> list[str]:
    """Report dangerous primitives in the generated JavaScript.

    Returns violation strings in the same shape as ``validate_html`` so they
    ride the existing surfacing path (trace line + critic instructions).
    """
    if not html:
        return []
    code = "\n".join(_executable_regions(html))
    if not code:
        return []
    return [
        f"security: {label} in generated script — artifacts must not reach outside the sandbox; remove it"
        for pattern, label in _HAZARDS
        if pattern.search(code)
    ]


def harden_html(html: str) -> str:
    """Inject the artifact CSP. Idempotent, and safe on already-sealed HTML."""
    if not html or not html.strip() or _CSP_MARKER in html:
        return html

    head = _HEAD_OPEN.search(html)
    if head is not None:
        pos = head.end()
    else:
        root = _HTML_OPEN.search(html)
        pos = root.end() if root is not None else 0

    # A <script> that the parser reaches before the meta would already have run
    # unrestricted, so never insert after one.
    first_script = _SCRIPT_OPEN.search(html)
    if first_script is not None and first_script.start() < pos:
        pos = first_script.start()

    return html[:pos] + _CSP_META + html[pos:]


def harden_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the artifact dict with every HTML payload hardened.

    Both the preview and the downloadable files are covered, which also keeps
    the `preview_html == files[entry].content` contract intact.
    """
    if not isinstance(artifact, dict):  # pragma: no cover — defensive
        return artifact

    hardened = dict(artifact)
    preview = hardened.get("preview_html")
    if isinstance(preview, str):
        hardened["preview_html"] = harden_html(preview)

    files = hardened.get("files")
    if isinstance(files, list):
        new_files = []
        for f in files:
            if isinstance(f, dict) and isinstance(f.get("content"), str) and _is_html_file(f):
                f = {**f, "content": harden_html(f["content"])}
            new_files.append(f)
        hardened["files"] = new_files

    return hardened


def _is_html_file(f: dict[str, Any]) -> bool:
    language = str(f.get("language", "")).lower()
    path = str(f.get("path", "")).lower()
    return language == "html" or path.endswith((".html", ".htm"))


# The depth floor: what separates a stub from an app. Met by EITHER bound.
#
# Lines alone misjudged dense output: the live re-measure of 2026-10-06
# (evals/orchestrator/reports/2026-10-06-recheck/r3-code-length/) flagged two
# complete apps — code.gen's 180 lines / 14.4 KB and code.critic's 191 lines /
# 16.9 KB — as "feature-incomplete". 200 readable lines run about 8 KB, so
# 12 000 characters is half again what the line bound already accepts and still
# 17% under the smallest complete app measured; a stub (a few KB, or a few
# lines padded out with blank ones — blank lines do not count) meets neither.
MIN_DEPTH_LINES = 200
MIN_DEPTH_SOURCE_CHARS = 12_000


def validate_html(html: str) -> list[str]:
    """
    Return a list of human-readable violation strings. Empty list = clean.

    Each entry is short enough to paste straight into a prompt.
    """
    v: list[str] = []

    if not html or not html.strip():
        return ["artifact is empty"]

    # Size / depth — met by either bound; see MIN_DEPTH_SOURCE_CHARS.
    lines = sum(1 for line in html.splitlines() if line.strip())
    if lines < MIN_DEPTH_LINES and len(html) < MIN_DEPTH_SOURCE_CHARS:
        v.append(
            f"under {MIN_DEPTH_LINES} lines ({lines}) and under {MIN_DEPTH_SOURCE_CHARS // 1000} KB "
            f"({len(html) / 1000:.1f} KB) — feature-incomplete, add depth"
        )

    # External asset violations (break sandbox, violate 'single-file' rule)
    for m in _EXTERNAL_SCRIPT.finditer(html):
        v.append(f'external <script src="{m.group(1)[:80]}"> — inline the JS instead')
    for m in _EXTERNAL_STYLESHEET.finditer(html):
        v.append(f'external <link stylesheet href="{m.group(1)[:80]}"> — inline the CSS')
    for m in _EXTERNAL_IMG.finditer(html):
        v.append(f'external <img src="{m.group(1)}{m.group(2)[:80]}"> — use inline SVG or data URI')

    # Structural
    if not _HAS_HTML.search(html):
        v.append("missing <html> tag")
    if not _HAS_HEAD.search(html):
        v.append("missing <head> tag")
    if not _HAS_BODY.search(html):
        v.append("missing <body> tag")
    if not _HAS_VIEWPORT.search(html):
        v.append('missing <meta name="viewport" content="width=device-width,initial-scale=1">')
    if not _HAS_SCRIPT.search(html):
        v.append("no <script> block — artifact isn't interactive")

    # Content-level hazards. DOWNGRADE, NOT REJECT — deliberate:
    #
    # * There is no reject path to use. Violations are advisory today: they are
    #   fed to code.critic and shown in the trace, and the artifact ships
    #   regardless. Making the validator throw would turn "the model wrote
    #   `fetch(` in a comment" into a failed, already-charged run.
    # * Rejecting on a heuristic hands the user a DoS lever on their own
    #   (paid) task, and a false positive costs the whole deliverable — the
    #   worst possible trade for a demo product.
    # * The real control is deterministic, not heuristic: harden_artifact()
    #   injects a CSP that blocks egress whether or not this scan matched. So
    #   the scan's job is visibility (trace) + repair (the critic is told to
    #   remove each hit), not gatekeeping.
    v.extend(scan_script_hazards(html))

    return v
