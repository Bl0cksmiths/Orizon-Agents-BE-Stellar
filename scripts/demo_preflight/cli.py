"""Command line for the demo pre-flight. See `scripts/demo_preflight/__init__.py`."""

from __future__ import annotations

import argparse
import math
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

import httpx
from stellar_sdk import StrKey

from .api import Reads
from .chain import ChainReader
from .checks import Check, Refused, run_checks
from .config import (
    DEFAULT_API,
    DEFAULT_BACKEND,
    DEFAULT_CAP,
    DEFAULT_FRONTEND,
    DEFAULT_MAX_REFUND,
    DEFAULT_TEAM_REGISTER,
    EXIT_REFUSED,
    TESTNET_HORIZON,
    TESTNET_RPC,
    WARMUP_BUDGET_SECONDS,
    RunConfig,
    normalize_base,
)
from .redact import SEED_SHAPE, Console
from .register import RegisterError
from .register import load as load_register
from .report import RunFacts, render_json, render_markdown, terminal_lines, verdict, write
from .retry import RetryPolicy


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.demo_preflight",
        description=(
            "GO / NO-GO for recording the 5.04 demo video on TESTNET: is escrow v2 deployed and settling, are "
            "refunds on, are the 5.02 routes live, is a real external operator bound and reachable, is a real agent "
            "below the reputation floor, are the buyer and operator funded, and does every page the script visits "
            "answer? Read-only; needs no secret. Every check names what to fix."
        ),
    )
    p.add_argument("--api", default=DEFAULT_API, help=f"the deployed API (default {DEFAULT_API})")
    p.add_argument("--frontend", default=DEFAULT_FRONTEND, help=f"the deployed frontend (default {DEFAULT_FRONTEND})")
    p.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        help=f"the backend's own host, for the root-level /readiness the /api proxy does not forward "
        f"(default {DEFAULT_BACKEND})",
    )
    p.add_argument("--buyer", metavar="G...", help="the buyer wallet the recording pays from")
    p.add_argument("--operator", metavar="G...", help="the operator wallet the recording registers and binds from")
    p.add_argument(
        "--cap",
        type=float,
        default=DEFAULT_CAP,
        help=f"the most the recorded plan may cost the buyer, in the network's asset (default {DEFAULT_CAP})",
    )
    p.add_argument(
        "--max-refund",
        type=float,
        default=DEFAULT_MAX_REFUND,
        help=f"MAX_REFUND_USDC as the Render dashboard has it (default {DEFAULT_MAX_REFUND}, the backend's default)",
    )
    p.add_argument(
        "--allow-team-operator",
        action="store_true",
        help="accept a buyer or operator from the team register; the report then says it must be disclosed on camera",
    )
    p.add_argument(
        "--with-decompose",
        metavar="INTENT",
        help="also decompose INTENT and confirm the plan excludes or substitutes the below-floor agent "
        "(off by default: it costs a model call and stores a plan)",
    )
    p.add_argument(
        "--team-register",
        type=Path,
        default=DEFAULT_TEAM_REGISTER,
        help=f"the committed register of wallets the team controls (default {DEFAULT_TEAM_REGISTER})",
    )
    p.add_argument("--rpc-url", default=TESTNET_RPC, help=f"Soroban RPC (default {TESTNET_RPC})")
    p.add_argument("--horizon-url", default=TESTNET_HORIZON, help=f"Horizon (default {TESTNET_HORIZON})")
    p.add_argument("--out-dir", type=Path, help="write demo-preflight.md and demo-preflight.json here")
    return p


def _account(value: str | None, flag: str) -> str | None:
    """A G... account, or None. A pasted seed is refused without being echoed."""
    if value is None:
        return None
    if SEED_SHAPE.match(value.strip()):
        raise ValueError(f"{flag} takes a public G... address; what was given is a SECRET seed (not shown). Rotate it.")
    if not StrKey.is_valid_ed25519_public_key(value.strip()):
        raise ValueError(f"{flag} must be a Stellar account address (G...), got {value!r}")
    return value.strip()


def _amount(value: float, flag: str) -> float:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{flag} must be a finite amount of zero or more, got {value!r}")
    return value


def _config(args: argparse.Namespace) -> RunConfig:
    intent = args.with_decompose.strip() if args.with_decompose is not None else None
    if intent is not None and len(intent) < 3:
        raise ValueError("--with-decompose needs an intent of at least three characters")
    return RunConfig(
        api=normalize_base(args.api, "--api", strip_api=True),
        frontend=normalize_base(args.frontend, "--frontend"),
        backend=normalize_base(args.backend, "--backend"),
        rpc_url=normalize_base(args.rpc_url, "--rpc-url"),
        horizon_url=normalize_base(args.horizon_url, "--horizon-url"),
        team_register=args.team_register,
        buyer=_account(args.buyer, "--buyer"),
        operator=_account(args.operator, "--operator"),
        cap=_amount(args.cap, "--cap"),
        max_refund=_amount(args.max_refund, "--max-refund"),
        allow_team_operator=args.allow_team_operator,
        decompose_intent=intent,
        out_dir=args.out_dir,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    stream: TextIO | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], float] | None = None,
    warmup_budget: float = WARMUP_BUDGET_SECONDS,
) -> int:
    console = Console(stream)
    args = build_parser().parse_args(argv)
    try:
        cfg = _config(args)
        team = load_register(cfg.team_register)
    except (ValueError, RegisterError) as exc:
        console.say(f"REFUSED: {exc}")
        return EXIT_REFUSED

    do_sleep = sleep or time.sleep
    retry = RetryPolicy(sleep=do_sleep)

    def progress(check: Check) -> None:
        for line in terminal_lines(check):
            console.say(line)

    console.say(f"demo pre-flight · api {cfg.api} · backend {cfg.backend} · frontend {cfg.frontend}")
    with httpx.Client(transport=transport) as http:
        reads = Reads(
            client=http,
            api=cfg.api,
            backend=cfg.backend,
            frontend=cfg.frontend,
            retry=retry,
            sleep=do_sleep,
            clock=clock or time.monotonic,
        )
        chain = ChainReader(client=http, rpc_url=cfg.rpc_url, horizon_url=cfg.horizon_url, retry=retry)
        try:
            checks, facts = run_checks(cfg, reads, chain, team, warmup_budget=warmup_budget, progress=progress)
        except Refused as exc:
            console.say(f"REFUSED: {exc}")
            return EXIT_REFUSED

    code, line = verdict(checks)
    stamp = now() if now else time.time()
    run = RunFacts(
        api=cfg.api,
        backend=cfg.backend,
        frontend=cfg.frontend,
        rpc_url=cfg.rpc_url,
        horizon_url=cfg.horizon_url,
        generated_at=int(stamp),
        generated_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
    )
    if cfg.out_dir is not None:
        md_path, json_path = write(
            cfg.out_dir,
            render_markdown(checks, facts, run, code, line),
            render_json(checks, facts, run, code, line),
        )
        console.say(f"wrote {md_path} and {json_path}")
    for disclosure in facts.disclosures:
        console.say(f"DISCLOSE: {disclosure}")
    console.say(f"exit {code}: {line}")
    return code
