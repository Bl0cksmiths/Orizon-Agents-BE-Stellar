"""Fit a model's draft into a worker's bounded output, in code.

Claude's structured outputs enforce a schema's shape — types, required fields,
enums — but not string lengths, list sizes or number ranges. The SDK's
`transform_schema` moves those into field descriptions and `app/llm/claude.py`
validates them after the call, so asking Claude for a bounded schema turns a
long-but-good answer into a failed step (research.pro failed 3/3 in the live
eval of 2026-10-06 on 223–236-character claims against a 200 cap). Workers
therefore ask for an unbounded draft and fit it here, the way the prompt
improver's `normalize_spec` does.

Trimming keeps whole sentences where it can, else whole words with an
ellipsis, and never ends inside a bracketed citation — "(Smith, 2021)" or
"[3]" is either kept whole or dropped whole. URLs never split, because a word
cut only happens at a space.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

ELLIPSIS = "…"

_OPENERS = {"(": ")", "[": "]"}
_SENTENCE_ENDS = ".!?"

# A sentence boundary further back than this share of the limit throws away
# too much; below it, a word cut keeps more of the text.
_MIN_SENTENCE_SHARE = 0.5

T = TypeVar("T")


def _open_bracket_at(text: str, end: int) -> int | None:
    """Index of the outermost bracket still open at `end`, or None."""
    stack: list[tuple[str, int]] = []
    for i, ch in enumerate(text[:end]):
        if ch in _OPENERS:
            stack.append((_OPENERS[ch], i))
        elif stack and ch == stack[-1][0]:
            stack.pop()
    return stack[0][1] if stack else None


def _sentence_cut(text: str, limit: int) -> int | None:
    """End of the last whole sentence that fits in `limit`, outside brackets."""
    best: int | None = None
    closers: list[str] = []
    for i, ch in enumerate(text[:limit]):
        if ch in _OPENERS:
            closers.append(_OPENERS[ch])
        elif closers and ch == closers[-1]:
            closers.pop()
        elif ch in _SENTENCE_ENDS and not closers and (i + 1 == len(text) or text[i + 1] == " "):
            best = i + 1
    if best is not None and best >= limit * _MIN_SENTENCE_SHARE:
        return best
    return None


def trim_text(text: str, limit: int) -> str:
    """`text` with whitespace collapsed, fitted into `limit` characters."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    sentence = _sentence_cut(text, limit)
    if sentence is not None:
        return text[:sentence]
    budget = limit - len(ELLIPSIS)
    cut = text.rfind(" ", 0, budget + 1)
    if cut > 0:
        opened = _open_bracket_at(text, cut)
        if opened is not None:
            cut = opened
    kept = text[:cut].rstrip(" ,;:-–—") if cut > 0 else ""
    if not kept:
        # One unbreakable run (no space before the budget, or only a bracket):
        # a hard cut is the only way to stay inside the bound.
        kept = text[:budget]
    return kept + ELLIPSIS


def trim_items(items: Sequence[str], limit: int | None = None) -> list[str]:
    """Non-blank items, each collapsed (and trimmed to `limit` when given)."""
    out: list[str] = []
    for item in items:
        fitted = trim_text(item, limit) if limit is not None else " ".join(item.split())
        if fitted:
            out.append(fitted)
    return out


def clamp(value: float, low: float, high: float) -> float:
    """`value` held inside [low, high]."""
    return min(max(value, low), high)


def at_most(items: Sequence[T], count: int) -> list[T]:
    """The first `count` items."""
    return list(items[:count])
