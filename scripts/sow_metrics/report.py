"""The three outputs: the frozen metrics block, a Markdown table, and the raw counted and excluded items.

The block is what the frontend's `content/evidence/index.json` `metrics`
array is pasted from, so its shape is frozen and checked here before anything
is written (`block_problems`): eleven entries m01..m11 in SOW order, the SOW's
wording verbatim, a plain-language reason on every miss, and every link an
https URL whose label is words — never a bare hash or address. Explorer links
are exactly `https://stellar.expert/explorer/testnet/{tx,contract,account}/<id>`.

The block carries no timestamp, so two runs against an unchanged chain and
deployment write byte-identical blocks; when it was measured is in the raw
JSON and the Markdown.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .collect import Snapshot
from .config import BLOCK_NAME, EXPLORER, MARKDOWN_NAME, RAW_NAME, SOW_ROWS
from .metrics import LINK_KINDS, MET, NOT_MET, Metric

BLOCK_KEYS = ("id", "category", "metric", "target", "achieved", "status", "reason", "method", "links")
LINK_KEYS = ("label", "url", "kind", "tx_hash", "date")
EXPLORER_URL = {
    "tx": re.compile(re.escape(EXPLORER) + r"/tx/[0-9a-f]{64}"),
    "contract": re.compile(re.escape(EXPLORER) + r"/contract/C[A-Z2-7]{55}"),
    "account": re.compile(re.escape(EXPLORER) + r"/account/G[A-Z2-7]{55}"),
}
# What a label must never contain: a Stellar strkey or a transaction hash.
_ADDRESS = re.compile(r"\b[GCM][A-Z2-7]{55}\b")
_HASH = re.compile(r"\b[0-9a-fA-F]{64}\b")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class RunFacts:
    api: str
    backend: str
    frontend: str
    rpc_url: str
    horizon_url: str
    github_api: str
    generated_at: int  # unix seconds
    generated_utc: str


def render_block(metrics: list[Metric]) -> list[dict[str, Any]]:
    return [m.block() for m in metrics]


def label_problem(label: Any) -> str | None:
    if not isinstance(label, str) or not label.strip():
        return "is empty"
    if _ADDRESS.search(label):
        return "contains a Stellar address"
    if _HASH.search(label):
        return "contains a transaction hash"
    return None


def block_problems(block: Any) -> list[str]:
    """Everything wrong with a block against the frozen shape; [] when it may be pasted."""
    problems: list[str] = []
    if not isinstance(block, list) or len(block) != len(SOW_ROWS):
        return [f"the block must be a list of {len(SOW_ROWS)} metrics"]
    for row, entry in zip(SOW_ROWS, block, strict=True):
        where = f"{row.id}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: not an object")
            continue
        keys = tuple(entry)
        expected = BLOCK_KEYS if entry.get("status") == NOT_MET else tuple(k for k in BLOCK_KEYS if k != "reason")
        if keys != expected:
            problems.append(f"{where}: keys {keys} are not {expected}")
        for key, want in (("id", row.id), ("category", row.category), ("metric", row.metric), ("target", row.target)):
            if entry.get(key) != want:
                problems.append(f"{where}: {key} {entry.get(key)!r} is not the SOW's {want!r}")
        if entry.get("status") not in (MET, NOT_MET):
            problems.append(f"{where}: status {entry.get('status')!r}")
        for key in ("achieved", "method"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                problems.append(f"{where}: {key} is empty")
        if entry.get("status") == NOT_MET and (not isinstance(entry.get("reason"), str) or not entry["reason"].strip()):
            problems.append(f"{where}: a miss needs a reason")
        links = entry.get("links")
        if not isinstance(links, list):
            problems.append(f"{where}: links is not a list")
            continue
        for i, link in enumerate(links):
            problems.extend(f"{where} link {i}: {p}" for p in link_problems(link))
    return problems


def link_problems(link: Any) -> list[str]:
    if not isinstance(link, dict):
        return ["not an object"]
    out = []
    keys = tuple(link)
    if keys[:3] != LINK_KEYS[:3] or any(k not in LINK_KEYS for k in keys):
        out.append(f"keys {keys}")
    kind, url = link.get("kind"), link.get("url")
    if kind not in LINK_KINDS:
        out.append(f"kind {kind!r}")
    if not isinstance(url, str) or not url.startswith("https://"):
        out.append(f"url {url!r} is not https")
    elif kind in EXPLORER_URL and not EXPLORER_URL[kind].fullmatch(url):
        out.append(f"url {url!r} is not a testnet explorer {kind} link")
    problem = label_problem(link.get("label"))
    if problem:
        out.append(f"label {problem}")
    if "tx_hash" in link:
        if kind != "tx" or not re.fullmatch(r"[0-9a-f]{64}", str(link["tx_hash"])):
            out.append("tx_hash must be 64 hex on a tx link")
        elif not str(url).endswith("/" + str(link["tx_hash"])):
            out.append("tx_hash does not match the url")
    if kind == "tx" and "tx_hash" not in link:
        out.append("a tx link carries its tx_hash")
    if "date" in link and not _DATE.fullmatch(str(link["date"])):
        out.append(f"date {link['date']!r} is not YYYY-MM-DD")
    return out


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(metrics: list[Metric], run: RunFacts, code: int) -> str:
    met = sum(1 for m in metrics if m.status == MET)
    unmeasured = [m.row.id for m in metrics if not m.measured]
    out = [
        "# SOW §6.3 success metrics (story 5.05)",
        "",
        f"**{met} of {len(metrics)} met.**"
        + (f" Not measured this run: {', '.join(unmeasured)}." if unmeasured else "")
        + f" (exit {code})",
        "",
        f"Measured {run.generated_utc} on Stellar testnet, read-only: API `{run.api}`, backend `{run.backend}`, "
        f"frontend `{run.frontend}`, RPC `{run.rpc_url}`, Horizon `{run.horizon_url}`, GitHub `{run.github_api}`.",
        "",
        "| # | Category | Metric (SOW) | Target | Achieved | Status |",
        "|---|---|---|---|---|---|",
    ]
    for m in metrics:
        status = "Met" if m.status == MET else "Not met"
        out.append(
            "| "
            + " | ".join(_cell(v) for v in (m.row.id, m.row.category, m.row.metric, m.row.target, m.achieved))
            + f" | **{status}** |"
        )
    for m in metrics:
        out += ["", f"## {m.row.id}. {m.row.metric}", ""]
        out.append(
            f"- **Target** {m.row.target}; **achieved** {m.achieved}; **{'met' if m.status == MET else 'not met'}**."
        )
        if m.reason and m.status == NOT_MET:
            out.append(f"- **Why not:** {m.reason}")
        out.append(f"- **How it was measured:** {m.method}")
        out.append(f"- **Counted:** {len(m.counted)}; **excluded:** {len(m.excluded)}.")
        if m.links:
            out.append("- **Proof:**")
            out += [f"  - [{link.label}]({link.url})" for link in m.links]
    return "\n".join(out) + "\n"


def render_raw(metrics: list[Metric], snap: Snapshot, run: RunFacts, code: int) -> dict[str, Any]:
    return {
        "generated_at": run.generated_at,
        "generated_utc": run.generated_utc,
        "network": "testnet",
        "exit_code": code,
        "sources": {
            "api": run.api,
            "backend": run.backend,
            "frontend": run.frontend,
            "rpc": run.rpc_url,
            "horizon": run.horizon_url,
            "github": run.github_api,
            **snap.urls,
        },
        "contracts": snap.network.get("contracts"),
        "team_register": snap.team,
        "platform_keys": snap.platform,
        "failures": snap.failures,
        "warnings": snap.warnings,
        "summary": {
            "registered_agents": len(snap.agents),
            "escrows": [
                {
                    "contract": e.contract,
                    "version": e.version,
                    "live": e.live,
                    "ids_issued": e.nonce,
                    "authorizations": len(e.auths),
                    "receipts": len(e.receipts),
                    "unreadable_ids": e.unreadable_ids,
                }
                for e in snap.escrows
            ],
            "ratings_by_kind": snap.rating_kinds,
            "dispute_ratings": len(snap.dispute_ratings),
            "ledger_lifetime_disputes": sum(snap.ledger_disputed.values()),
            "platform_transfers": [asdict(t) for t in snap.transfers],
        },
        "metrics": [
            {
                "id": m.row.id,
                "metric": m.row.metric,
                "achieved": m.achieved,
                "status": m.status,
                "measured": m.measured,
                "unmeasured_because": m.unmeasured,
                "counted": m.counted,
                "excluded": m.excluded,
            }
            for m in metrics
        ],
    }


def dump_block(block: list[dict[str, Any]]) -> str:
    return json.dumps(block, indent=2, ensure_ascii=False) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write(out_dir: Path, block: list[dict[str, Any]], markdown: str, raw: dict[str, Any]) -> tuple[Path, Path, Path]:
    paths = (out_dir / BLOCK_NAME, out_dir / MARKDOWN_NAME, out_dir / RAW_NAME)
    _atomic_write(paths[0], dump_block(block))
    _atomic_write(paths[1], markdown)
    _atomic_write(paths[2], json.dumps(raw, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n")
    return paths
