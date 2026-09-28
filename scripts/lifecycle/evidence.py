"""The evidence file story 5.05 is built from, and the state a rerun resumes from.

Two files per evidence directory, with different jobs:

`lifecycle.jsonl` — APPEND-ONLY JSON Lines, one row per thing that happened,
written (and fsynced) the moment it happened. A run that crashes in stage 7
keeps every hash from stages 1-6, because each was on disk before the next
request was sent. A rerun appends to the same file; nothing is ever rewritten.
`lifecycle.md` is re-rendered from the whole JSONL after every append, so the
human-readable table is never more than one row behind and never the source of
truth.

`state.json` — the run's working memory: which plan, which authorization,
which task, which dispute, the balances taken before the money moved. It is
what `--from-task` and `--from-dispute` resume from, and what stops a second
invocation from authorizing a second payment into a directory whose first one
is still in flight. It holds the task read token (a capability for this task's
reads, dead once the backend restarts), so it is written 0600 and is NOT
evidence: publish the JSONL and the Markdown, never this file.

Every on-chain status in a row is what the ledger answered when asked, never
what the harness expected — see `chain.ChainReader.observe`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import EXPLORER_TX
from .redact import Redactor

JSONL_NAME = "lifecycle.jsonl"
MARKDOWN_NAME = "lifecycle.md"
STATE_NAME = "state.json"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class EvidenceRow:
    """One thing that happened. `tx_hash` rows carry the ledger's own status."""

    stage: str
    event: str
    utc: str
    network: str
    run_id: str
    tx_hash: str | None = None
    explorer: str | None = None
    contract: str | None = None
    agent: str | None = None
    buyer: str | None = None
    amount: str | None = None
    asset: str | None = None
    onchain_status: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    seq: int = 0


def tx_row(
    *,
    stage: str,
    event: str,
    network: str,
    run_id: str,
    tx_hash: str,
    onchain_status: str,
    contract: str | None = None,
    agent: str | None = None,
    buyer: str | None = None,
    amount: str | None = None,
    asset: str | None = None,
    detail: dict[str, Any] | None = None,
) -> EvidenceRow:
    return EvidenceRow(
        stage=stage,
        event=event,
        utc=utc_now(),
        network=network,
        run_id=run_id,
        tx_hash=tx_hash,
        explorer=EXPLORER_TX.format(tx_hash),
        contract=contract,
        agent=agent,
        buyer=buyer,
        amount=amount,
        asset=asset,
        onchain_status=onchain_status,
        detail=detail or {},
    )


class EvidenceLog:
    """Append-only JSONL plus its Markdown rendering, in one directory.

    Every row is written through the run's redactor, like every printed line:
    a row quoting a server's error message may quote whatever the server
    echoed back, and a FastAPI validation error echoes its input.
    """

    def __init__(self, directory: Path, redactor: Redactor | None = None) -> None:
        self.directory = directory
        self.redactor = redactor
        self.jsonl = directory / JSONL_NAME
        self.markdown = directory / MARKDOWN_NAME

    def rows(self) -> list[dict[str, Any]]:
        """Every row on disk. A torn LAST line — the process died mid-write —
        is skipped rather than fatal, so a crash can never make the file it
        left behind unreadable."""
        if not self.jsonl.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def append(self, row: EvidenceRow) -> EvidenceRow:
        self.directory.mkdir(parents=True, exist_ok=True)
        row.seq = len(self.rows()) + 1
        line = json.dumps(asdict(row), sort_keys=True, ensure_ascii=False)
        if self.redactor is not None:
            line = self.redactor.scrub(line)
        # A torn last line (a crash mid-write) has no newline; without one here
        # this row would be glued onto it and lost with it.
        torn = self.jsonl.exists() and self.jsonl.stat().st_size > 0 and not self.jsonl.read_bytes().endswith(b"\n")
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if torn else "") + line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.render()
        return row

    def render(self) -> None:
        _atomic_write(self.markdown, render_markdown(self.rows()))


def _short(tx_hash: str) -> str:
    return f"{tx_hash[:8]}…{tx_hash[-6:]}"


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(rows: list[dict[str, Any]]) -> str:
    """The evidence as three tables: transactions, reputation, and every stage."""
    lines = ["# Lifecycle evidence (story 5.01)", ""]
    if rows:
        first = rows[0]
        lines += [
            f"Network: **{first.get('network')}** · first row {first.get('utc')} · last row {rows[-1].get('utc')}",
            "",
            "Generated from `lifecycle.jsonl` after every stage; the JSONL is the source of truth.",
            "Every on-chain status below was read back from Soroban RPC or Horizon, never assumed.",
            "",
        ]

    lines += [
        "## Transactions",
        "",
        "| # | Run | Stage | Event | UTC | Tx | On-chain status | Contract | Agent | Buyer | Amount |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if not r.get("tx_hash"):
            continue
        amount = f"{r.get('amount')} {r.get('asset') or ''}".strip() if r.get("amount") else ""
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    r.get("seq"),
                    r.get("run_id"),
                    r.get("stage"),
                    r.get("event"),
                    r.get("utc"),
                    f"[{_short(r['tx_hash'])}]({r.get('explorer')})",
                    r.get("onchain_status"),
                    r.get("contract"),
                    r.get("agent"),
                    r.get("buyer"),
                    amount,
                )
            )
            + " |"
        )

    lines += [
        "",
        "## Reputation snapshots",
        "",
        "| # | Label | UTC | Agent | Score (bps) | Lower bound (bps) | Source | Count | Disputed | Stale |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.get("event") != "reputation_snapshot":
            continue
        d = r.get("detail") or {}
        lines.append(
            "| "
            + " | ".join(
                _cell(v)
                for v in (
                    r.get("seq"),
                    d.get("label"),
                    r.get("utc"),
                    r.get("agent"),
                    d.get("smoothed_bps"),
                    d.get("lower_bound_bps"),
                    d.get("source"),
                    d.get("count"),
                    d.get("disputed"),
                    d.get("stale"),
                )
            )
            + " |"
        )

    lines += ["", "## Every row", "", "| # | Stage | Event | UTC | Summary |", "|---|---|---|---|---|"]
    for r in rows:
        summary = (r.get("detail") or {}).get("summary") or r.get("onchain_status") or ""
        lines.append(
            "| "
            + " | ".join(_cell(v) for v in (r.get("seq"), r.get("stage"), r.get("event"), r.get("utc"), summary))
            + " |"
        )
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


@dataclass
class RunState:
    """The run's working memory. See the module docstring for why it is not evidence."""

    run_id: str = ""
    agent: str = ""
    buyer: str = ""
    network: str = ""
    escrow_version: int | None = None
    plan: dict[str, Any] | None = None
    # A signed-but-unconfirmed authorize is recorded HERE before it is sent,
    # so a crash between the send and the answer leaves the hash to look up.
    authorize: dict[str, Any] | None = None
    task_id: str | None = None
    read_token: str | None = None
    start_ledger: int | None = None
    balances_before: dict[str, int] | None = None
    # agent id -> the owner account its payouts land in, read from the live
    # registry before the money moved.
    owners: dict[str, str] | None = None
    settlement: dict[str, Any] | None = None
    dispute: dict[str, Any] | None = None
    completed: list[str] = field(default_factory=list)
    stopped: dict[str, Any] | None = None

    def done(self, stage: str) -> None:
        if stage not in self.completed:
            self.completed.append(stage)


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.path = directory / STATE_NAME

    def load(self) -> RunState | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        known = {k: v for k, v in raw.items() if k in RunState.__dataclass_fields__}
        return RunState(**known)

    def save(self, state: RunState) -> None:
        _atomic_write(self.path, json.dumps(asdict(state), indent=2, sort_keys=True), mode=0o600)
        # The evidence dir is meant to be committed (docs/evidence/...); the
        # state file is not, so the directory says so itself.
        ignore = self.path.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text(f"# the harness's working state holds a task read token; not evidence\n{STATE_NAME}\n")
