"""Hashes given on the command line: the ones the video's browser takes produced.

The video's key moments are driven in the browser — the operator registers,
the buyer authorizes, the step settles, the buyer disputes and is refunded —
so their hashes never pass through the lifecycle harness. They are given here
instead, and verified exactly as harness rows are:

    --tx KIND=HASH[:label]       repeatable, e.g. --tx register=<hash>:"Operator registers research.pro"
    --rows browser.json          a JSON list of {"kind", "tx_hash", "label"?, "deliverable"?, "network"?}

`HASH` may also be the transaction's Stellar Expert testnet link, as the
browser shows it. `kind` must be one of `config.KINDS` and maps to its
deliverable as a harness row's does; `deliverable` may only restate that, except
for an `other`, which may name any of D1–D4. A label must be plain language by
the evidence index's rule (`links.label_problem`). A row naming another
network, or a Stellar Expert link on another network, is refused, as a harness
row from another network is. A malformed hash is not refused: like a harness
row's, it is listed in the sheet as MALFORMED and never asked or published.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import DELIVERABLES, KIND_DELIVERABLE, KINDS, TESTNET
from .links import label_problem
from .rows import InputError, TxRow, WrongNetwork

STAGE = "browser"
ROW_KEYS = frozenset({"kind", "tx_hash", "label", "deliverable", "network"})

_EXPLORER = re.compile(r"^https?://stellar\.expert/explorer/(?P<net>[a-z]+)/tx/(?P<hash>[^/?#:\s]+)/?$")
# `KIND=<link>:label`: the link's own `https:` is not the label separator.
_LINK_VALUE = re.compile(r"^(?P<link>https?://[^\s]*?/tx/[^/?#:\s]+/?)(?::(?P<label>.*))?$", re.DOTALL)


def _hash(raw: str, where: str) -> str:
    value = raw.strip()
    link = _EXPLORER.match(value)
    if link:
        if link["net"] != TESTNET:
            raise WrongNetwork(
                f"{where}: a Stellar Expert link on {link['net']!r}; the video's evidence is testnet only"
            )
        return link["hash"].lower()
    if value.startswith(("http://", "https://")):
        raise InputError(f"{where}: {value!r} is not a Stellar Expert transaction link; give the hash")
    if not value:
        raise InputError(f"{where}: no transaction hash")
    return value.lower()


def _kind(raw: Any, where: str) -> str:
    kind = raw.strip().lower() if isinstance(raw, str) else raw
    if kind not in KINDS:
        raise InputError(f"{where}: kind {raw!r} is not one of {', '.join(KINDS)}")
    return str(kind)


def _label(raw: Any, where: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InputError(f"{where}: label must be a string, not {type(raw).__name__}")
    label = raw.strip()
    if not label:
        return None
    why = label_problem(label)
    if why:
        raise InputError(f"{where}: label {label!r} {why}")
    return label


def _deliverable(raw: Any, kind: str, where: str) -> str | None:
    if raw is None:
        return None
    if raw not in DELIVERABLES:
        raise InputError(f"{where}: deliverable {raw!r} is not one of {', '.join(DELIVERABLES)}")
    if kind != "other" and raw != KIND_DELIVERABLE[kind]:
        raise InputError(
            f"{where}: a {kind!r} is evidence for {KIND_DELIVERABLE[kind]}, not {raw}; only an 'other' names its own"
        )
    return str(raw)


def given_row(kind: str, tx_hash: str, label: str | None, deliverable: str | None, where: str) -> TxRow:
    return TxRow(
        source=where,
        run_id="",
        seq=None,
        stage=STAGE,
        event=kind,
        utc="",
        tx_hash=tx_hash,
        agent=None,
        amount=None,
        asset=None,
        contract=None,
        recorded_status=None,
        summary="",
        given_label=label,
        given_deliverable=deliverable,
    )


def parse_tx(arg: str, index: int) -> TxRow:
    """One `--tx KIND=HASH[:label]`."""
    where = f"--tx #{index}"
    kind_raw, sep, value = arg.partition("=")
    if not sep:
        raise InputError(f"{where}: {arg!r} is not KIND=HASH[:label]")
    kind = _kind(kind_raw, where)
    link = _LINK_VALUE.match(value.strip())
    if link:
        hash_raw, label_raw = link["link"], link["label"]
    else:
        hash_raw, _, label_raw = value.partition(":")
    return given_row(kind, _hash(hash_raw, where), _label(label_raw, where), None, where)


def load_rows_file(path: Path) -> list[TxRow]:
    """Every row of one `--rows` file, in file order."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"{path}: cannot be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: not JSON: {exc}") from exc
    if not isinstance(body, list):
        raise InputError(f"{path}: must be a JSON list of {{kind, tx_hash, label?, deliverable?}}")
    rows: list[TxRow] = []
    for i, raw in enumerate(body):
        where = f"{path}[{i}]"
        if not isinstance(raw, dict):
            raise InputError(f"{where}: must be an object, not {type(raw).__name__}")
        unknown = sorted(set(raw) - ROW_KEYS)
        if unknown:
            raise InputError(f"{where}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(sorted(ROW_KEYS))}")
        network = raw.get("network", TESTNET)
        if network != TESTNET:
            raise WrongNetwork(f"{where}: a row from network {network!r}; the video's evidence is testnet only")
        kind = _kind(raw.get("kind"), where)
        tx_raw = raw.get("tx_hash")
        if not isinstance(tx_raw, str):
            raise InputError(f"{where}: tx_hash must be a string, not {tx_raw!r}")
        rows.append(
            given_row(
                kind,
                _hash(tx_raw, where),
                _label(raw.get("label"), where),
                _deliverable(raw.get("deliverable"), kind, where),
                where,
            )
        )
    return rows


def load_given(tx_args: list[str], rows_files: list[Path]) -> list[TxRow]:
    """The `--rows` files' rows in the order given, then the `--tx` hashes in the order given."""
    rows: list[TxRow] = []
    for path in rows_files:
        rows.extend(load_rows_file(path))
    rows.extend(parse_tx(arg, i) for i, arg in enumerate(tx_args, start=1))
    return rows
