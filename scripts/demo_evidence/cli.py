"""Command line for the evidence sheet. See `scripts/demo_evidence/__init__.py`."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

import httpx

from .chain import MALFORMED, READ_ERRORS, UNREADABLE, ChainReader, Verdict
from .config import (
    EXIT_NOT_SUCCESS,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    TESTNET_HORIZON,
    TESTNET_PASSPHRASE,
    TESTNET_RPC,
    RunConfig,
)
from .given import load_given
from .links import render_index_links, utc_date
from .redact import Console
from .render import Entry, render_description, render_json, render_sheet, write, write_json
from .retry import RetryPolicy
from .rows import InputError, Loaded, TxRow, WrongNetwork, load, merge

DEFAULT_TITLE = "Orizon Agents — Blue Belt demo (Stellar testnet)"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.demo_evidence",
        description=(
            "Turn the lifecycle harness's evidence files from the recording runs, and the hashes the browser "
            "takes produced, into the video's evidence: evidence-sheet.md, description.txt and the /demo page's "
            "evidence.json. Every hash is re-verified read-only on TESTNET; one that is not SUCCESS is listed as "
            "failed and never published."
        ),
    )
    p.add_argument("inputs", nargs="*", type=Path, help="lifecycle.jsonl files, or the evidence dirs holding them")
    p.add_argument(
        "--tx",
        action="append",
        default=[],
        metavar="KIND=HASH[:label]",
        help="a hash the browser produced (repeatable); HASH may be its Stellar Expert testnet link",
    )
    p.add_argument(
        "--rows",
        action="append",
        default=[],
        type=Path,
        metavar="FILE.json",
        help='a JSON list of {"kind", "tx_hash", "label"?, "deliverable"?} (repeatable)',
    )
    p.add_argument(
        "--index-links",
        type=Path,
        metavar="FILE.json",
        help="also write every verified hash as an evidence index link, grouped by index item",
    )
    p.add_argument("--out-dir", required=True, type=Path, help="write the three outputs here")
    p.add_argument("--title", default=DEFAULT_TITLE, help="the video's title, for the sheet and the description")
    p.add_argument(
        "--disclose",
        action="append",
        default=[],
        metavar="SENTENCE",
        help="a sentence appended to the description's limitations (e.g. a team operator the pre-flight named)",
    )
    p.add_argument("--rpc-url", default=TESTNET_RPC, help=f"Soroban RPC (default {TESTNET_RPC})")
    p.add_argument("--horizon-url", default=TESTNET_HORIZON, help=f"Horizon (default {TESTNET_HORIZON})")
    return p


def _url(raw: str, flag: str) -> str:
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"{flag} must be an absolute http(s) URL, got {raw!r}")
    return url


def _refusal(reader: ChainReader) -> str | None:
    try:
        rpc = reader.rpc_passphrase()
        horizon = reader.horizon_passphrase()
    except READ_ERRORS as exc:
        return f"could not confirm the network (a refusal, not a guess): {exc}"
    if rpc != TESTNET_PASSPHRASE:
        return f"the RPC's network is {rpc!r}; the video's evidence is testnet only"
    if horizon != TESTNET_PASSPHRASE:
        return f"Horizon's network is {horizon!r}; the video's evidence is testnet only"
    return None


def outcome(entries: list[Entry], undated: int = 0) -> tuple[int, str]:
    bad = [e for e in entries if not e.verified]
    unreadable = [e for e in bad if e.verdict.status == UNREADABLE]
    contradicted = [e for e in bad if e.verdict.status != UNREADABLE]
    ok = len(entries) - len(bad)
    if contradicted:
        return EXIT_NOT_SUCCESS, f"{len(contradicted)} hash(es) are not SUCCESS on the ledger; {ok} published"
    if unreadable:
        return EXIT_UNREADABLE, f"{len(unreadable)} hash(es) could not be read; rerun; {ok} published"
    if undated:
        return EXIT_UNREADABLE, f"{undated} verified hash(es) have no ledger date, so no index link; rerun"
    return EXIT_OK, f"all {ok} hash(es) re-verified SUCCESS"


def _inputs(cfg: RunConfig) -> Loaded:
    if not (cfg.inputs or cfg.tx_args or cfg.rows_files):
        raise InputError("nothing to read: give lifecycle.jsonl files, --rows or --tx")
    given = load_given(list(cfg.tx_args), list(cfg.rows_files))
    harness = load(list(cfg.inputs), require_rows=not (cfg.tx_args or cfg.rows_files))
    return merge(harness, given, cfg.rows_files, len(cfg.tx_args))


def _dated(reader: ChainReader, entries: list[Entry], console: Console) -> tuple[list[tuple[TxRow, str]], int]:
    """Each verified row with the UTC date of its ledger on Horizon; a row whose date cannot be read is left out."""
    dated: list[tuple[TxRow, str]] = []
    undated = 0
    for e in entries:
        if not e.verified:
            continue
        created = e.verdict.created_at
        if created is None:
            try:
                created = reader.created_at(e.row.tx_hash)
            except READ_ERRORS as exc:
                console.say(f"[BAD] {e.row.tx_hash}: its ledger date could not be read ({type(exc).__name__})")
                undated += 1
                continue
        date = utc_date(created) if created else None
        if date is None:
            console.say(f"[BAD] {e.row.tx_hash}: Horizon gave no ledger date ({created!r}); no index link")
            undated += 1
            continue
        dated.append((e.row, date))
    return dated, undated


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    stream: TextIO | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> int:
    console = Console(stream)
    args = build_parser().parse_args(argv)
    try:
        cfg = RunConfig(
            inputs=tuple(args.inputs),
            out_dir=args.out_dir,
            rpc_url=_url(args.rpc_url, "--rpc-url"),
            horizon_url=_url(args.horizon_url, "--horizon-url"),
            title=args.title.strip() or DEFAULT_TITLE,
            disclosures=tuple(d.strip() for d in args.disclose if d.strip()),
            tx_args=tuple(args.tx),
            rows_files=tuple(args.rows),
            index_links=args.index_links,
        )
        loaded = _inputs(cfg)
    except (ValueError, InputError, WrongNetwork) as exc:
        console.say(f"REFUSED: {exc}")
        return EXIT_REFUSED

    retry = RetryPolicy(sleep=sleep or time.sleep)
    entries: list[Entry] = []
    with httpx.Client(transport=transport) as http:
        reader = ChainReader(client=http, rpc_url=cfg.rpc_url, horizon_url=cfg.horizon_url, retry=retry)
        refusal = _refusal(reader)
        if refusal:
            console.say(f"REFUSED: {refusal}")
            return EXIT_REFUSED
        for row in loaded.rows:
            if row.well_formed:
                verdict = reader.verify(row.tx_hash)
            else:
                verdict = Verdict(row.tx_hash, MALFORMED, None, "none", "not a 64-hex transaction hash")
            entries.append(Entry(row, verdict))
            mark = "ok " if entries[-1].verified else "BAD"
            console.say(f"[{mark}] {row.deliverable} {row.kind:<14} {row.tx_hash} -> {verdict.status} ({row.source})")
        dated, undated = _dated(reader, entries, console) if cfg.index_links else ([], 0)

    generated_at = int(now() if now else time.time())
    paths = write(
        cfg.out_dir,
        render_sheet(entries, loaded, generated_at, cfg.title),
        render_description(entries, generated_at, cfg.title, cfg.disclosures, loaded.open_disputes),
        render_json(entries, generated_at),
    )
    if cfg.index_links:
        paths.append(write_json(cfg.index_links, render_index_links(dated, generated_at)))
    code, line = outcome(entries, undated)
    console.say(f"wrote {', '.join(str(p) for p in paths)}")
    console.say(f"exit {code}: {line}")
    return code
