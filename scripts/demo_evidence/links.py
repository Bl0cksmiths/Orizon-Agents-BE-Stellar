"""The index-links output: every verified transaction as a link of the frontend's evidence index.

Story 5.01's rule is that every hash goes into the evidence index
(`content/evidence/index.json` in the frontend) as it is produced. This file
is that hand-off, in the index's own link shape, grouped by the index item
each transaction is evidence for (`config.KIND_INDEX_ITEM`):

    {"schema": "orizon.evidence-index-links/1", "network": "testnet", "generated_at": <unix>,
     "items": [{"id": "6.1-D4-d",
                "links": [{"label": "Settlement for agent alpha of 0.0100000 XLM on Stellar testnet — 2026-09-24",
                           "url": "https://stellar.expert/explorer/testnet/tx/<hash>",
                           "kind": "tx", "tx_hash": "<64 hex>", "date": "2026-09-24"}]}]}

Items come in the index's order; links in the order the sheet lists them.
`date` is the UTC calendar date of the ledger's `created_at` on Horizon, which
is what the index's snapshot method says its dates are. A label is plain
language by the index validator's own rule (`label_problem`, ported from the
frontend's `lib/evidence/validate.mjs`): at least two real words, never a
bare hash or address, and any full hash or address inside it is shortened.
Only a hash that re-verified SUCCESS is ever a link.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .config import EXPLORER_TX, INDEX_ITEMS, INDEX_LINKS_SCHEMA, KIND_INDEX_ITEM, KIND_LABEL, TESTNET
from .redact import scrub
from .rows import TxRow

# `lib/evidence/validate.mjs`, rule for rule.
_BARE_HEX = re.compile(r"^(?:0x)?[0-9a-f]{8,}$", re.IGNORECASE)
_SHORT_HEX = re.compile(r"^[0-9a-f]{4,}\s*(?:…|\.{2,3})\s*[0-9a-f]{4,}$", re.IGNORECASE)
_BARE_STRKEY = re.compile(r"^[GCM][A-Z2-7]{55}$")
_SHORT_STRKEY = re.compile(r"^[GCM][A-Z2-7]{2,}\s*(?:…|\.{2,3})\s*[A-Z2-7]{3,}$")
_EDGES = re.compile(r"^[\W_]+|[\W_]+$")  # anything but a letter or a digit, at either end of a token
_WORD = re.compile(r"[^\W\d_]{2,}")  # two letters in a row

# A full id inside a label, which is shortened the way the index writes them.
_FULL_HEX = re.compile(r"\b[0-9a-f]{64}\b")
_FULL_STRKEY = re.compile(r"\b[GCM][A-Z2-7]{55}\b")


def _is_bare_id(token: str) -> bool:
    return any(p.match(token) for p in (_BARE_HEX, _SHORT_HEX, _BARE_STRKEY, _SHORT_STRKEY))


def label_problem(label: str) -> str | None:
    """Why a link label is not plain language, or None when it is (the frontend validator's `labelProblem`)."""
    text = label.strip()
    if not text:
        return "must be a non-empty string"
    if _is_bare_id(text):
        return "is a bare hash or address; say in words what the link shows"
    words = [t for t in (_EDGES.sub("", tok) for tok in text.split()) if _WORD.search(t) and not _is_bare_id(t)]
    if len(words) < 2:
        return "must be at least two words of plain language, not just an id"
    return None


def shorten_ids(text: str) -> str:
    """Every full hash as `abcd1234…89abcdef` and every full account or contract id as `GBI2I…ADBH`."""
    text = _FULL_HEX.sub(lambda m: f"{m.group(0)[:8]}…{m.group(0)[-8:]}", text)
    return _FULL_STRKEY.sub(lambda m: f"{m.group(0)[:5]}…{m.group(0)[-4:]}", text)


def utc_date(created_at: str) -> str | None:
    """The UTC calendar date of a Horizon `created_at`, or None when it is not a timestamp with a zone."""
    try:
        stamp = datetime.fromisoformat(created_at.strip())
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(UTC).date().isoformat()


def index_label(row: TxRow, date: str) -> str:
    if row.given_label:
        head = row.given_label.strip()
    else:
        head = KIND_LABEL[row.kind]
        if row.agent:
            head += f" for agent {row.agent}"
        if row.amount:
            head += f" of {row.amount} {row.asset or ''}".rstrip()
        head += " on Stellar testnet"
    return shorten_ids(scrub(f"{head} — {date}"))


def index_link(row: TxRow, date: str) -> dict[str, str]:
    return {
        "label": index_label(row, date),
        "url": EXPLORER_TX.format(row.tx_hash),
        "kind": "tx",
        "tx_hash": row.tx_hash,
        "date": date,
    }


def render_index_links(dated: list[tuple[TxRow, str]], generated_at: int) -> dict[str, Any]:
    """The verified rows, each with its ledger date, grouped by index item in the index's order."""
    groups: dict[str, list[dict[str, str]]] = {item: [] for item in INDEX_ITEMS}
    for row, date in dated:
        groups[KIND_INDEX_ITEM[row.kind]].append(index_link(row, date))
    return {
        "schema": INDEX_LINKS_SCHEMA,
        "network": TESTNET,
        "generated_at": generated_at,
        "items": [{"id": item, "links": links} for item, links in groups.items() if links],
    }
