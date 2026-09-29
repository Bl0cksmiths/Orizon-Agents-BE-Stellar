"""Reading the lifecycle harness's evidence files into the transactions the video shows.

Each input is a `lifecycle.jsonl` (or a directory holding one), in the row
shape `scripts/lifecycle/evidence.py` writes: one JSON object per line with
`stage`, `event`, `utc`, `network`, `run_id`, `seq`, and — on a transaction
row — `tx_hash`, `explorer`, `contract`, `agent`, `buyer`, `amount`, `asset`,
`onchain_status` and a `detail` dict. Only rows with a `tx_hash` become
transactions; the rest (notes, reputation snapshots) are context.

The network is refused, not judged: a transaction row that does not say
`testnet`, or any row naming another network, stops the run before a hash is
read. A file from a mainnet run next to a testnet one would put a hash in the
video's description that resolves on the wrong explorer, or nowhere.

A torn line — the harness died mid-write — is skipped and counted, exactly as
the harness's own reader skips it. The same hash in two rows (a resumed run
re-recording what it read back) is kept once, at its first appearance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import EVENT_KINDS, JSONL_NAME, KIND_DELIVERABLE, KIND_LABEL, KIND_PROVES, TESTNET, TX_HASH

# A row written before the harness read the network (`Runner.note` falls back
# to "unknown") says nothing about it either way.
_NO_NETWORK = frozenset({"", "unknown"})


class InputError(Exception):
    """An input cannot be read, or holds no transaction."""


class WrongNetwork(Exception):
    """A row is from a network other than testnet."""


@dataclass(frozen=True)
class TxRow:
    source: str  # "<file>:<line>", for every message that names the row
    run_id: str
    seq: int | None
    stage: str
    event: str
    utc: str
    tx_hash: str
    agent: str | None
    amount: str | None
    asset: str | None
    contract: str | None
    recorded_status: str | None  # what the harness read back when it wrote the row
    summary: str
    # A hash given on the command line (`--tx`, `--rows`) rather than by the
    # harness: the operator's own label, and a deliverable for an `other`.
    given_label: str | None = None
    given_deliverable: str | None = None

    @property
    def kind(self) -> str:
        return EVENT_KINDS.get(self.event, "other")

    @property
    def deliverable(self) -> str:
        return self.given_deliverable or KIND_DELIVERABLE[self.kind]

    @property
    def proves(self) -> str:
        return KIND_PROVES[self.kind]

    @property
    def well_formed(self) -> bool:
        return bool(TX_HASH.match(self.tx_hash))

    @property
    def label(self) -> str:
        if self.given_label:
            return self.given_label
        parts = [KIND_LABEL[self.kind]]
        if self.agent:
            parts.append(f"— {self.agent}")
        if self.amount:
            parts.append(f"({self.amount} {self.asset or ''})".replace(" )", ")"))
        return " ".join(parts)


@dataclass(frozen=True)
class Loaded:
    rows: list[TxRow]
    files: list[Path]
    skipped_lines: int
    duplicates: int
    given_files: tuple[Path, ...] = ()  # `--rows` files merged in
    given_tx: int = 0  # `--tx` hashes merged in


def _file(path: Path) -> Path:
    candidate = path / JSONL_NAME if path.is_dir() else path
    if not candidate.is_file():
        raise InputError(f"{path}: no {JSONL_NAME} here")
    return candidate


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def load(paths: list[Path], *, require_rows: bool = True) -> Loaded:
    """Every transaction row of every input, in input order then file order. Raises on another network.

    `require_rows=False` when hashes are also given on the command line, which
    `merge` adds; the run then needs a row from either, not from both.
    """
    files = [_file(p) for p in paths]
    rows: list[TxRow] = []
    seen: set[str] = set()
    skipped = 0
    duplicates = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise InputError(f"{path}: cannot be read: {exc}") from exc
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            where = f"{path}:{number}"
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(raw, dict):
                skipped += 1
                continue
            network = str(raw.get("network") or "")
            tx_hash = raw.get("tx_hash")
            if network not in _NO_NETWORK and network != TESTNET:
                raise WrongNetwork(f"{where}: a row from network {network!r}; the video's evidence is testnet only")
            if not tx_hash:
                continue
            if network != TESTNET:
                raise WrongNetwork(
                    f"{where}: a transaction row whose network is {network or 'unset'!r}, not testnet; refusing to "
                    "publish a hash whose network the row does not name"
                )
            tx_hash = str(tx_hash).strip().lower()
            if tx_hash in seen:
                duplicates += 1
                continue
            seen.add(tx_hash)
            raw_detail = raw.get("detail")
            detail: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
            seq = raw.get("seq")
            rows.append(
                TxRow(
                    source=where,
                    run_id=str(raw.get("run_id") or ""),
                    seq=seq if isinstance(seq, int) else None,
                    stage=str(raw.get("stage") or ""),
                    event=str(raw.get("event") or ""),
                    utc=str(raw.get("utc") or ""),
                    tx_hash=tx_hash,
                    agent=_str(raw.get("agent")),
                    amount=_str(raw.get("amount")),
                    asset=_str(raw.get("asset")),
                    contract=_str(raw.get("contract")),
                    recorded_status=_str(raw.get("onchain_status")),
                    summary=str(detail.get("summary") or ""),
                )
            )
    if not rows and require_rows:
        raise InputError(f"no transaction rows in {', '.join(str(f) for f in files)}")
    return Loaded(rows=rows, files=files, skipped_lines=skipped, duplicates=duplicates)


def merge(loaded: Loaded, given: list[TxRow], given_files: tuple[Path, ...], given_tx: int) -> Loaded:
    """The harness's rows, then the given ones, each hash kept once at its first appearance.

    A hash filed as two different kinds is refused: one of the two labels would
    be wrong in the video's description, and the tool cannot tell which.
    """
    rows = list(loaded.rows)
    first = {r.tx_hash: r for r in rows}
    duplicates = loaded.duplicates
    for row in given:
        prior = first.get(row.tx_hash)
        if prior is not None:
            if prior.kind != row.kind:
                raise InputError(
                    f"{row.source}: {row.tx_hash} is given as {row.kind!r}, but {prior.source} filed it as "
                    f"{prior.kind!r}; give each hash one kind"
                )
            duplicates += 1
            continue
        first[row.tx_hash] = row
        rows.append(row)
    if not rows:
        sources = [str(f) for f in (*loaded.files, *given_files)] + (["--tx"] if given_tx else [])
        raise InputError(f"no transaction rows in {', '.join(sources) or 'the input'}")
    return Loaded(
        rows=rows,
        files=loaded.files,
        skipped_lines=loaded.skipped_lines,
        duplicates=duplicates,
        given_files=given_files,
        given_tx=given_tx,
    )
