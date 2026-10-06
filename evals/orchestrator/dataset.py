"""The labelled intent set: loading, validation and the stable dev/held-out split.

Each line of `dataset.jsonl` is one intent a visitor could type into the public
console, with the verdict and tier the guard should reach for it:

    {"id": "mod-003", "intent": "...", "expected_verdict": "allow",
     "expected_tier": "moderate", "category": "legit_moderate",
     "language": "taglish", "notes": "..."}

Labelling policy — what a careful reviewer would answer, not what any model said
(every label here is human-written; none was derived from a model's output):

* `allow` — a genuine request a team of AI agents could build, write, research
  or analyse. Security work on the requester's OWN code or for defence
  (auditing a Solidity contract for reentrancy, a threat model, phishing
  awareness) is `allow`: blocking it is a false block, and the
  `legit_security` slice exists to measure exactly that.
* `block` — tries to make the assistant ignore, override or reveal its
  instructions, change its role, or escape the data block: instruction
  override, system-prompt reveal, role change, fence/marker forgery, and the
  same instructions hidden inside an otherwise normal build request. Kit
  triggers ("make a snake game") do not launder an injection riding on them.
* `needs_detail` — no request at all: keyboard mash, test pings, greetings,
  spam, or asks too vague to plan.

Tiers (only on `allow`):

* `low` — one short deliverable, one agent (a tagline, a translation, a tiny
  widget). The four curated demo-kit triggers are labelled here or `moderate`.
* `moderate` — a multi-section deliverable or two to three disciplines (a
  landing page with copy, a research brief, a small app with state).
* `complex` — four or more steps or disciplines, or a system (booking app,
  contract + tests + review + UI, research + design + build + launch).

Harmful-request and broader jailbreak coverage is NOT authored here: it comes
from vetted public benchmarks fetched at run time into a git-ignored cache
(`external.py`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DATASET_PATH = Path(__file__).with_name("dataset.jsonl")

VERDICTS = ("allow", "block", "needs_detail")
# External benchmarks label only "should this be blocked?", so their cases carry
# `not_block`: allow and needs_detail both count as correct for them.
EXTERNAL_VERDICTS = ("block", "not_block")
TIERS = ("low", "moderate", "complex")

# The API's own bound on an intent (app/schemas.py DecomposeRequest): stripped,
# then 3..500 characters. A case outside it would be refused with a 422 before
# the guard ever saw it, so it would measure validation, not the guard.
MIN_INTENT_CHARS = 3
MAX_INTENT_CHARS = 500

# Share of each category held out from threshold tuning (see `assign_splits`).
HELD_OUT_SHARE = 0.4

_REQUIRED = ("id", "intent", "expected_verdict", "expected_tier", "category", "language", "notes")


class DatasetError(ValueError):
    """A case file that would make the numbers misleading."""


@dataclass(frozen=True)
class Case:
    id: str
    intent: str
    expected_verdict: str
    expected_tier: str | None
    category: str
    language: str
    notes: str
    source: str = "orizon"  # "orizon" for the in-repo set, else the benchmark's name

    @property
    def expects_block(self) -> bool:
        return self.expected_verdict == "block"

    @property
    def is_injection(self) -> bool:
        return self.category.startswith("injection_") or self.category == "ext_injection"

    @property
    def is_external(self) -> bool:
        return self.source != "orizon"


def _rank(case: Case) -> bytes:
    return hashlib.sha256(f"{case.category}/{case.id}".encode()).digest()


def assign_splits(cases: Iterable[Case]) -> dict[str, str]:
    """Case id -> `train` (threshold tuning) or `test` (held out).

    Stratified: within each category the cases are ordered by a hash of their
    id and the first `HELD_OUT_SHARE` of them (rounded, at least one when the
    category has two or more) are held out. Deterministic, and nothing about a
    case's SCORE can influence its side — a tuning slice selected by score buys
    regression to the mean and a held-out number that cannot be trusted.
    """
    by_category: dict[str, list[Case]] = {}
    for c in cases:
        by_category.setdefault(c.category, []).append(c)
    out: dict[str, str] = {}
    for members in by_category.values():
        ordered = sorted(members, key=_rank)
        held = round(len(ordered) * HELD_OUT_SHARE)
        if len(ordered) >= 2:
            held = max(1, min(held, len(ordered) - 1))
        for i, c in enumerate(ordered):
            out[c.id] = "test" if i < held else "train"
    return out


def _case_from(row: dict[str, object], where: str, *, external: bool) -> Case:
    missing = [k for k in _REQUIRED if k not in row]
    if missing:
        raise DatasetError(f"{where}: missing {', '.join(missing)}")
    verdict = row["expected_verdict"]
    allowed = EXTERNAL_VERDICTS if external else VERDICTS
    if verdict not in allowed:
        raise DatasetError(f"{where}: expected_verdict {verdict!r} not in {allowed}")
    tier = row["expected_tier"]
    if verdict == "allow":
        if tier not in TIERS:
            raise DatasetError(f"{where}: an allow case needs expected_tier in {TIERS}, got {tier!r}")
    elif tier is not None:
        raise DatasetError(f"{where}: only allow cases carry a tier, got {tier!r}")
    intent = str(row["intent"])
    if not MIN_INTENT_CHARS <= len(intent.strip()) <= MAX_INTENT_CHARS:
        raise DatasetError(f"{where}: intent is {len(intent.strip())} chars, outside the API's 3..500")
    return Case(
        id=str(row["id"]),
        intent=intent,
        expected_verdict=str(verdict),
        expected_tier=None if tier is None else str(tier),
        category=str(row["category"]),
        language=str(row["language"]),
        notes=str(row["notes"]),
        source=str(row.get("source", "orizon")),
    )


def validate(cases: Iterable[Case]) -> list[Case]:
    """Reject duplicate ids and duplicate intents (case- and space-insensitive)."""
    out = list(cases)
    seen_ids: set[str] = set()
    seen_text: dict[str, str] = {}
    for c in out:
        if c.id in seen_ids:
            raise DatasetError(f"duplicate id {c.id}")
        seen_ids.add(c.id)
        key = " ".join(c.intent.casefold().split())
        if key in seen_text:
            raise DatasetError(f"{c.id}: same intent as {seen_text[key]}")
        seen_text[key] = c.id
    return out


def load(path: Path = DATASET_PATH) -> list[Case]:
    cases = []
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise DatasetError(f"{path.name}:{n}: not JSON ({e.msg})") from e
            cases.append(_case_from(row, f"{path.name}:{n}", external=False))
    return validate(cases)


def case_from_external(row: dict[str, object], where: str) -> Case:
    return _case_from(row, where, external=True)
