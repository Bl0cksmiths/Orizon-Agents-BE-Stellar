"""Command line for the adoption report. See `scripts/adoption_report/__init__.py`."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

import httpx
from stellar_sdk import StrKey

from .api import AdoptionApi, ApiUnreachable, ShapeError, parse
from .chain import ChainReader
from .config import (
    DEFAULT_TEAM_REGISTER,
    EXIT_API_UNREADABLE,
    EXIT_CHAIN_UNREADABLE,
    EXIT_CLAIM_FAILED,
    EXIT_COUNT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_TARGET_NOT_MET,
    TESTNET_HORIZON,
    TESTNET_PASSPHRASE,
    TESTNET_RPC,
    RunConfig,
    normalize_api_base,
)
from .register import RegisterError
from .register import load as load_register
from .report import RunFacts, render_json, render_markdown, write
from .retry import RetryPolicy
from .verify import CONTRADICTED, READ_ERRORS, UNREADABLE, Verification, met, summary_line, verify


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.adoption_report",
        description=(
            "Re-verify every claim GET /api/ecosystem/adoption makes — external owners, their agents, their "
            "settled workflows — against TESTNET Horizon and Soroban RPC, recount the SOW §6.3 totals from the "
            "verified facts, and write a Markdown + JSON evidence report (story 5.02). Read-only; needs no secret."
        ),
    )
    p.add_argument(
        "--api", required=True, help="the deployed base, e.g. https://orizons.xyz (the /api prefix is added)"
    )
    p.add_argument("--escrow", required=True, metavar="C...", help="the PaymentEscrow contract id to verify against")
    p.add_argument("--registry", required=True, metavar="C...", help="the AgentRegistry contract id to verify against")
    p.add_argument("--rpc-url", default=TESTNET_RPC, help=f"Soroban RPC (default {TESTNET_RPC})")
    p.add_argument("--horizon-url", default=TESTNET_HORIZON, help=f"Horizon (default {TESTNET_HORIZON})")
    p.add_argument(
        "--team-register",
        type=Path,
        default=DEFAULT_TEAM_REGISTER,
        help=f"the committed register of wallets the Blocksmiths control (default {DEFAULT_TEAM_REGISTER})",
    )
    p.add_argument("--out-dir", type=Path, help="write adoption-report.md and adoption-report.json here")
    p.add_argument(
        "--require-met",
        action="store_true",
        help=f"also exit {EXIT_TARGET_NOT_MET} when everything verified but a target is NOT MET",
    )
    return p


def _url(raw: str, flag: str) -> str:
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"{flag} must be an absolute http(s) URL, got {raw!r}")
    return url


def _config(args: argparse.Namespace) -> RunConfig:
    for flag, value in (("--escrow", args.escrow), ("--registry", args.registry)):
        if not StrKey.is_valid_contract(value):
            raise ValueError(f"{flag} must be a contract id (C...), got {value!r}")
    return RunConfig(
        api=normalize_api_base(args.api),
        rpc_url=_url(args.rpc_url, "--rpc-url"),
        horizon_url=_url(args.horizon_url, "--horizon-url"),
        escrow=args.escrow,
        registry=args.registry,
        team_register=args.team_register,
        out_dir=args.out_dir,
        require_met=args.require_met,
    )


def _refuse_unless_testnet(reader: ChainReader) -> str | None:
    """A refusal message, or None when both RPC and Horizon are testnet."""
    try:
        rpc = reader.rpc_passphrase()
        horizon = reader.horizon_passphrase()
    except READ_ERRORS as exc:
        return f"could not confirm the network (a refusal, not a guess): {exc}"
    if rpc != TESTNET_PASSPHRASE:
        return f"the RPC's network is {rpc!r}; this verifier reads testnet only ({TESTNET_PASSPHRASE!r})"
    if horizon != TESTNET_PASSPHRASE:
        return f"Horizon's network is {horizon!r}; this verifier reads testnet only ({TESTNET_PASSPHRASE!r})"
    return None


def outcome(result: Verification, require_met: bool) -> tuple[int, str]:
    failures = result.claim_failures()
    contradicted = [c for c in failures if c.kind == CONTRADICTED]
    unreadable = [c for c in failures if c.kind == UNREADABLE]
    if contradicted:
        return EXIT_CLAIM_FAILED, f"FAILED — {len(contradicted)} claim check(s) do not hold on the chain"
    if unreadable:
        return EXIT_CHAIN_UNREADABLE, f"UNVERIFIED — {len(unreadable)} chain read(s) failed; rerun"
    if result.count_failures():
        return EXIT_COUNT_MISMATCH, "MISCOUNTED — every claim held, but the API's totals disagree with the recount"
    if require_met and not all(met(result.recount).values()):
        return EXIT_TARGET_NOT_MET, "VERIFIED, and a target is NOT MET"
    return EXIT_OK, "VERIFIED — every claim holds on the chain and the recount agrees with the API"


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    stream: TextIO | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> int:
    out = stream if stream is not None else sys.stdout

    def say(line: str = "") -> None:
        out.write(line + "\n")
        out.flush()

    args = build_parser().parse_args(argv)
    try:
        cfg = _config(args)
        team = load_register(cfg.team_register)
    except (ValueError, RegisterError) as exc:
        say(f"REFUSED: {exc}")
        return EXIT_REFUSED

    retry = RetryPolicy(sleep=sleep or time.sleep)
    with httpx.Client(transport=transport, follow_redirects=True) as http:
        reader = ChainReader(client=http, rpc_url=cfg.rpc_url, horizon_url=cfg.horizon_url, retry=retry)
        refusal = _refuse_unless_testnet(reader)
        if refusal:
            say(f"REFUSED: {refusal}")
            return EXIT_REFUSED

        api = AdoptionApi(client=http, base=cfg.api, retry=retry)
        try:
            claim = parse(api.fetch())
        except ApiUnreachable as exc:
            say(f"API UNREADABLE: {exc}")
            return EXIT_API_UNREADABLE
        except ShapeError as exc:
            say(f"API SHAPE: {api.url} is not the frozen adoption shape: {exc}")
            return EXIT_API_UNREADABLE
        if claim.network != "testnet":
            say(f"REFUSED: the API reports network {claim.network!r}; this verifier reads testnet only")
            return EXIT_REFUSED

        result = verify(claim, reader, registry=cfg.registry, escrow=cfg.escrow, team=team)

    code, verdict = outcome(result, cfg.require_met)
    run = RunFacts(
        api_url=api.url,
        rpc_url=cfg.rpc_url,
        horizon_url=cfg.horizon_url,
        escrow=cfg.escrow,
        registry=cfg.registry,
        generated_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now() if now else None)),
        exit_code=code,
        verdict=verdict,
    )
    markdown = render_markdown(claim, result, team, run)
    say(markdown.rstrip("\n"))
    if cfg.out_dir is not None:
        md_path, json_path = write(cfg.out_dir, markdown, render_json(claim, result, team, run))
        say(f"\nwrote {md_path} and {json_path}")
    for check in result.claim_failures() + result.count_failures():
        say(f"FAIL: {summary_line(check)}")
    say(f"exit {code}: {verdict}")
    return code
