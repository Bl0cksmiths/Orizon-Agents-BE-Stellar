"""Command line for the SOW §6.3 metrics generator. See `scripts/sow_metrics/__init__.py`."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

import httpx
from stellar_sdk import StrKey

from .api import Reads
from .chain import ChainReader
from .collect import FAILURES, collect
from .config import (
    DEFAULT_API,
    DEFAULT_BACKEND,
    DEFAULT_FRONTEND,
    DEFAULT_GITHUB_API,
    DEFAULT_TEAM_REGISTER,
    EXIT_MEASURED,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    PAGE_METRICS,
    TESTNET_HORIZON,
    TESTNET_PASSPHRASE,
    TESTNET_RPC,
    PendingLink,
    RunConfig,
    normalize_base,
    parse_pending_link,
)
from .metrics import MET, measure
from .register import RegisterError
from .register import load as load_register
from .report import RunFacts, block_problems, dump_block, render_block, render_markdown, render_raw, write
from .retry import RetryPolicy


class Refused(Exception):
    """Not testnet, or the sources disagree about which network this is: nothing is measured."""


class NetworkUnreadable(Exception):
    """A network check got no answer, so which network this is cannot be confirmed."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.sow_metrics",
        description=(
            "Measure the eleven SOW §6.3 success metrics from Stellar TESTNET and the live deployment, and write the "
            "evidence index's frozen metrics block, a Markdown table and the raw counted and excluded items. "
            "Read-only; needs no secret. Exit 0 when every metric was measured, met or not."
        ),
    )
    p.add_argument("--api", default=DEFAULT_API, help=f"the deployed API (default {DEFAULT_API})")
    p.add_argument("--frontend", default=DEFAULT_FRONTEND, help=f"the deployed frontend (default {DEFAULT_FRONTEND})")
    p.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        help=f"the backend's own host, for /readiness and /openapi.json (default {DEFAULT_BACKEND})",
    )
    p.add_argument("--github-api", default=DEFAULT_GITHUB_API, help=f"GitHub's API (default {DEFAULT_GITHUB_API})")
    p.add_argument(
        "--escrow",
        action="append",
        default=[],
        metavar="C...",
        help="an escrow contract to count besides the live one and the known v1 (repeatable)",
    )
    p.add_argument(
        "--team-register",
        type=Path,
        default=DEFAULT_TEAM_REGISTER,
        help=f"the committed register of wallets the team controls (default {DEFAULT_TEAM_REGISTER})",
    )
    p.add_argument("--rpc-url", default=TESTNET_RPC, help=f"Soroban RPC (default {TESTNET_RPC})")
    p.add_argument("--horizon-url", default=TESTNET_HORIZON, help=f"Horizon (default {TESTNET_HORIZON})")
    p.add_argument(
        "--pending-link",
        action="append",
        default=[],
        metavar="ID=URL[=label]",
        help="for milestone ID (m06, m09 or m10): when its page answers 404, link this GitHub pull request instead "
        "(repeatable). Without it, a 404 page is not linked at all",
    )
    p.add_argument("--out-dir", type=Path, help="write the block, the Markdown and the raw JSON here")
    p.add_argument("--print-block", action="store_true", help="also print the frozen metrics block as JSON")
    return p


def _escrow(value: str) -> str:
    if not StrKey.is_valid_contract(value.strip()):
        raise ValueError(f"--escrow must be a contract id (C...), got {value!r}")
    return value.strip()


def _pending_links(values: list[str]) -> dict[str, PendingLink]:
    out: dict[str, PendingLink] = {}
    for raw in values:
        metric_id, link = parse_pending_link(raw)
        if metric_id in out:
            raise ValueError(f"--pending-link {metric_id} was given twice")
        out[metric_id] = link
    return out


def _config(args: argparse.Namespace) -> RunConfig:
    return RunConfig(
        api=normalize_base(args.api, "--api", strip_api=True),
        frontend=normalize_base(args.frontend, "--frontend"),
        backend=normalize_base(args.backend, "--backend"),
        rpc_url=normalize_base(args.rpc_url, "--rpc-url"),
        horizon_url=normalize_base(args.horizon_url, "--horizon-url"),
        github_api=normalize_base(args.github_api, "--github-api"),
        team_register=args.team_register,
        extra_escrows=tuple(_escrow(e) for e in args.escrow),
        out_dir=args.out_dir,
        pending_links=_pending_links(args.pending_link),
    )


def confirm_testnet(reads: Reads, chain: ChainReader) -> dict[str, object]:
    """The API's network document, once the RPC, Horizon and the API all say testnet."""
    try:
        rpc = chain.rpc_passphrase()
        horizon = chain.horizon_passphrase()
        answer = reads.network()
    except FAILURES as exc:
        raise NetworkUnreadable(f"{type(exc).__name__}: {exc}") from exc
    if not answer.ok or not answer.obj():
        raise NetworkUnreadable(f"{answer.url} answered HTTP {answer.status}")
    network = answer.obj()
    said = {
        "the RPC": rpc,
        "Horizon": horizon,
        "the API": network.get("network_passphrase"),
    }
    wrong = [f"{who} says {value!r}" for who, value in said.items() if value != TESTNET_PASSPHRASE]
    if wrong:
        raise Refused("this is not testnet: " + "; ".join(wrong))
    if network.get("network") != "testnet":
        raise Refused(f"the API names network {network.get('network')!r}, not testnet")
    return network


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    stream: TextIO | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> int:
    out = stream or sys.stdout

    def say(line: str) -> None:
        print(line, file=out)

    args = build_parser().parse_args(argv)
    try:
        cfg = _config(args)
        team = load_register(cfg.team_register)
    except (ValueError, RegisterError) as exc:
        say(f"REFUSED: {exc}")
        return EXIT_REFUSED

    retry = RetryPolicy(sleep=sleep or time.sleep)
    say(f"sow metrics · api {cfg.api} · backend {cfg.backend} · frontend {cfg.frontend} · testnet only, read-only")
    with httpx.Client(transport=transport) as http:
        reads = Reads(
            client=http, api=cfg.api, backend=cfg.backend, frontend=cfg.frontend, github_api=cfg.github_api, retry=retry
        )
        chain = ChainReader(client=http, rpc_url=cfg.rpc_url, horizon_url=cfg.horizon_url, retry=retry)
        try:
            network = confirm_testnet(reads, chain)
        except Refused as exc:
            say(f"REFUSED: {exc}")
            return EXIT_REFUSED
        except NetworkUnreadable as exc:
            say(f"UNREADABLE: the network could not be confirmed, so nothing was measured: {exc}")
            return EXIT_UNREADABLE
        snap = collect(dict(network), team, reads, chain, extra_escrows=cfg.extra_escrows)

    metrics = measure(snap, cfg.pending_links)
    code = EXIT_MEASURED if all(m.measured for m in metrics) else EXIT_UNREADABLE
    block = render_block(metrics)
    problems = block_problems(block)
    if problems:  # a generator bug, never a finding: refuse to write a block the index cannot take
        raise ValueError("the metrics block breaks its frozen shape: " + "; ".join(problems))

    stamp = now() if now else time.time()
    run = RunFacts(
        api=cfg.api,
        backend=cfg.backend,
        frontend=cfg.frontend,
        rpc_url=cfg.rpc_url,
        horizon_url=cfg.horizon_url,
        github_api=cfg.github_api,
        generated_at=int(stamp),
        generated_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
    )
    for m in metrics:
        status = "MET    " if m.status == MET else ("UNREAD " if not m.measured else "NOT MET")
        say(f"[{status}] {m.row.id} {m.row.metric} — target {m.row.target}, achieved {m.achieved}")
    for source, failure in sorted(snap.failures.items()):
        say(f"read failed: {source}: {failure}")
    for warning in snap.warnings:
        say(f"warning: {warning}")
    for metric_id in sorted(cfg.pending_links):
        page = snap.pages.get(PAGE_METRICS[metric_id])
        if page is None or page.status != 404:
            status = "could not be read" if page is None else f"answered HTTP {page.status}"
            say(f"note: --pending-link {metric_id} was not used: {PAGE_METRICS[metric_id]} {status}, not 404")
    if cfg.out_dir is not None:
        paths = write(cfg.out_dir, block, render_markdown(metrics, run, code), render_raw(metrics, snap, run, code))
        say("wrote " + ", ".join(str(p) for p in paths))
    if args.print_block:
        out.write(dump_block(block))
    met = sum(1 for m in metrics if m.status == MET)
    unread = sum(1 for m in metrics if not m.measured)
    summary = f"{met} of {len(metrics)} met" + (f", {unread} not measured" if unread else "")
    say(f"exit {code}: {summary}")
    return code
