"""The three outputs: the evidence sheet, the video description, and the /demo page's JSON.

`evidence.json` is a FROZEN shape — the frontend's /demo page reads it:

    {"generated_at": <unix>, "network": "testnet",
     "items": [{"label": str, "deliverable": "D1".."D4", "kind": <KINDS>,
                "tx_hash": <64 hex>, "explorer": "https://stellar.expert/explorer/testnet/tx/<hash>",
                "verified": true}]}

Only a hash that re-verified SUCCESS on the ledger is ever an item, so
`verified` is always true there; a hash that did not is listed in the sheet,
under its own heading, and nowhere else. The description lists the same
verified hashes as the JSON, in the same order.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chain import Verdict
from .config import DESCRIPTION_NAME, EXPLORER_TX, JSON_NAME, SHEET_NAME, TESTNET
from .redact import scrub
from .rows import Loaded, TxRow


@dataclass(frozen=True)
class Entry:
    row: TxRow
    verdict: Verdict

    @property
    def verified(self) -> bool:
        return self.row.well_formed and self.verdict.verified

    @property
    def explorer(self) -> str:
        return EXPLORER_TX.format(self.row.tx_hash)


# The limitations paragraph, one sentence at a time. Every sentence that is
# not true of every run is derived from what the run shows: a dispute's
# sentence is written only when a verified row, or the harness's own record,
# supports it. The asset sentence is the frontend's: since #92/#97 it labels
# amounts in the network's asset, "XLM" on testnet (`lib/trace-amounts.ts`,
# `lib/register-validation.ts`), and "usdc" survives only in wire field names.
TESTNET_SENTENCE = "Everything in this video runs on the Stellar TESTNET (SOW §3.6); no real value moved."
ASSET_SENTENCE = (
    "On testnet the escrow's asset contract wraps native XLM, so every amount is testnet XLM, and the interface "
    'labels it XLM; the "usdc" in some API field names is the field\'s name, not the asset.'
)
UPHELD_SENTENCE = (
    "A dispute was upheld by the platform's adjudicator key — a human decision behind an API key, not an on-chain "
    "arbiter — and its refund is a partial credit paid from the platform's signing key, which is also escrow v2's "
    "settler, not a clawback from the operator."
)
RATED_AFTER_REFUND = "The dispute's rating then landed on the ReputationLedger."
NOT_RATED_AFTER_REFUND = "No dispute rating is among the verified transactions, so the video claims none."
RATED_ONLY = (
    "A dispute rating landed on the ReputationLedger, but no refund is among the verified transactions, so the "
    "video claims none."
)
OPEN_SENTENCE = (
    "{noun} {ids} {verb} open when the run was recorded: no one had adjudicated {it}, so the video claims no refund "
    "and no dispute rating for {it}."
)
PRIOR_SENTENCE = (
    "Reputation scores are prior-smoothed, so an agent's lower bound near the floor can move with a single rating."
)
AUDIT_SENTENCE = "The contracts have not been externally audited."


def dispute_sentences(entries: list[Entry], open_disputes: tuple[str, ...] = ()) -> list[str]:
    """What the run shows of disputes, and nothing more.

    A verified `refund` is what shows an uphold: the credit is only ever paid
    on one. A verified `dispute_rating` shows the rating and nothing else. A
    dispute the harness last recorded as open is said to be open. With none of
    these there is no sentence at all.
    """
    kinds = {e.row.kind for e in entries if e.verified}
    out: list[str] = []
    if "refund" in kinds:
        out += [UPHELD_SENTENCE, RATED_AFTER_REFUND if "dispute_rating" in kinds else NOT_RATED_AFTER_REFUND]
    elif "dispute_rating" in kinds:
        out.append(RATED_ONLY)
    if open_disputes:
        one = len(open_disputes) == 1
        out.append(
            OPEN_SENTENCE.format(
                noun="Dispute" if one else "Disputes",
                ids=", ".join(open_disputes),
                verb="was" if one else "were",
                it="it" if one else "them",
            )
        )
    return out


def limitations(entries: list[Entry], open_disputes: tuple[str, ...], disclosures: tuple[str, ...]) -> str:
    sentences = [
        TESTNET_SENTENCE,
        ASSET_SENTENCE,
        *dispute_sentences(entries, open_disputes),
        PRIOR_SENTENCE,
        AUDIT_SENTENCE,
        *disclosures,
    ]
    return "Limitations. " + " ".join(sentences)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _utc(stamp: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def render_json(entries: list[Entry], generated_at: int) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "network": TESTNET,
        "items": [
            {
                "label": scrub(e.row.label),
                "deliverable": e.row.deliverable,
                "kind": e.row.kind,
                "tx_hash": e.row.tx_hash,
                "explorer": e.explorer,
                "verified": True,
            }
            for e in entries
            if e.verified
        ],
    }


def _sources(loaded: Loaded) -> str:
    named = [f"`{f}`" for f in (*loaded.files, *loaded.given_files)]
    if loaded.given_tx:
        named.append(f"{loaded.given_tx} hash(es) given with `--tx`")
    return ", ".join(named)


def render_sheet(entries: list[Entry], loaded: Loaded, generated_at: int, title: str) -> str:
    verified = [e for e in entries if e.verified]
    failed = [e for e in entries if not e.verified]
    out = [
        f"# Evidence sheet — {title}",
        "",
        f"Generated {_utc(generated_at)} from {_sources(loaded)}. Network: **testnet**.",
        "",
        f"**{len(verified)} of {len(entries)} transaction(s) re-verified SUCCESS** on the ledger just now "
        "(Soroban RPC `getTransaction`, falling back to Horizon). Only those are in `evidence.json` and "
        "`description.txt`.",
        "",
    ]
    if loaded.skipped_lines or loaded.duplicates:
        out += [
            f"Skipped {loaded.skipped_lines} unreadable line(s) and {loaded.duplicates} repeated hash(es) "
            "in the input.",
            "",
        ]
    out += [
        "## Transactions, in the order the video shows them",
        "",
        "| # | Stage | What it proves | Deliverable | Tx hash | Stellar Expert | Recorded | Re-verified |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, e in enumerate(entries, start=1):
        reverified = e.verdict.status if e.row.well_formed else "MALFORMED"
        mark = "" if e.verified else " ✗"
        out.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    i,
                    f"{e.row.stage} · {e.row.label}",
                    e.row.proves,
                    e.row.deliverable,
                    f"`{e.row.tx_hash}`",
                    f"[open]({e.explorer})" if e.row.well_formed else "—",
                    e.row.recorded_status or "—",
                    f"{reverified} ({e.verdict.source}){mark}",
                )
            )
            + " |"
        )
    if failed:
        out += [
            "",
            "## Failed verification — NOT in evidence.json or the description",
            "",
        ]
        for e in failed:
            why = "the hash is not 64 hex characters" if not e.row.well_formed else e.verdict.note or e.verdict.status
            out.append(
                f"- `{e.row.tx_hash}` ({e.row.label}, {e.row.source}): **{e.verdict.status}** — {why}. "
                "Rerun the stage that produced it, or cut it from the video."
            )
    return scrub("\n".join(out) + "\n")


def render_description(
    entries: list[Entry],
    generated_at: int,
    title: str,
    disclosures: tuple[str, ...],
    open_disputes: tuple[str, ...] = (),
) -> str:
    verified = [e for e in entries if e.verified]
    out = [
        title,
        "",
        "[One or two sentences on what the video shows — fill in before publishing.]",
        "",
        "Chapters",
        "00:00 [CHAPTERS — replace with the timestamps from the edit]",
        "",
        f"On-chain evidence — every transaction below is on the Stellar testnet and was re-verified SUCCESS on "
        f"{_utc(generated_at)[:10]}:",
        "",
    ]
    for i, e in enumerate(verified, start=1):
        out += [f"{i}. {e.row.label} ({e.row.deliverable})", f"   {e.row.tx_hash}", f"   {e.explorer}"]
    out += ["", limitations(verified, open_disputes, disclosures), ""]
    return scrub("\n".join(out))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, body: dict[str, Any]) -> Path:
    _atomic_write(path, json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    return path


def write(out_dir: Path, sheet: str, description: str, body: dict[str, Any]) -> list[Path]:
    paths = [out_dir / SHEET_NAME, out_dir / DESCRIPTION_NAME, out_dir / JSON_NAME]
    _atomic_write(paths[0], sheet)
    _atomic_write(paths[1], description)
    _atomic_write(paths[2], json.dumps(body, indent=2) + "\n")
    return paths
