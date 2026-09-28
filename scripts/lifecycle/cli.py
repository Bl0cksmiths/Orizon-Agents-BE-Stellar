"""Command line for the lifecycle harness. See docs/operators/lifecycle-harness.md."""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

import httpx

from .api import OrizonApi
from .config import EXIT_REFUSED, STAGES, TESTNET_HORIZON, Budgets, RunConfig, normalize_api_base
from .evidence import EvidenceLog, StateStore
from .redact import Console, Redactor
from .retry import RetryPolicy
from .stages import Runner

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A seed is ALSO a valid variable name (upper-case base32), so it is refused by
# shape before the name rule is asked.
_SEED_SHAPE = re.compile(r"^S[A-Z2-7]{55}$")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m scripts.lifecycle",
        description=(
            "Drive the deployed Orizon API through the whole buyer lifecycle on TESTNET — decompose, "
            "authorize, execute, settle, seal, dispute, uphold, refund, reputation — and record every "
            "transaction hash, read back from the ledger, into an append-only evidence file (story 5.01)."
        ),
    )
    p.add_argument(
        "--api", required=True, help="the deployed base, e.g. https://orizons.xyz (the /api prefix is added)"
    )
    p.add_argument("--agent", required=True, help="the external agent id the plan must route to")
    p.add_argument("--intent", help="what the buyer asks for; required for a fresh run")
    p.add_argument(
        "--buyer-secret-env",
        required=True,
        metavar="NAME",
        help="NAME of the environment variable holding the buyer's S... seed (never the seed itself)",
    )
    p.add_argument(
        "--adjudicator-key-env",
        metavar="NAME",
        help="NAME of the environment variable holding the operator API key; needed to run through 'uphold'",
    )
    p.add_argument("--evidence-dir", required=True, type=Path, help="one directory per run: JSONL, Markdown, state")
    p.add_argument("--dry-run", action="store_true", help="do every read, build and sign nothing, print the plan")
    p.add_argument("--until", choices=STAGES, default=STAGES[-1], help="stop after this stage (default: all)")
    resume = p.add_mutually_exclusive_group()
    resume.add_argument("--from-task", metavar="TASK_ID", help="resume at 'poll' for this task (e.g. after a restart)")
    resume.add_argument("--from-dispute", metavar="DISPUTE_ID", help="resume at 'uphold' for this dispute")
    p.add_argument("--dispute-reason", help="the buyer's reason on the dispute (default: a harness sentence)")
    p.add_argument("--rpc-url", help="override the Soroban RPC the API names in /api/stellar/network")
    p.add_argument(
        "--horizon-url", default=TESTNET_HORIZON, help=f"Horizon for history reads (default {TESTNET_HORIZON})"
    )
    return p


def _env_name(value: str | None, flag: str) -> str | None:
    """An environment variable NAME. A value that is not one — above all, a
    pasted seed — is refused without being echoed."""
    if value is None:
        return None
    if _SEED_SHAPE.match(value) or not _ENV_NAME.match(value):
        raise ValueError(f"{flag} takes the NAME of an environment variable, and what was given is not one (not shown)")
    return value


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    environ: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    budgets: Budgets | None = None,
) -> int:
    import os

    args = build_parser().parse_args(argv)
    console = Console(Redactor(), stream)
    try:
        buyer_env = _env_name(args.buyer_secret_env, "--buyer-secret-env")
        adjudicator_env = _env_name(args.adjudicator_key_env, "--adjudicator-key-env")
        base = normalize_api_base(args.api)
    except ValueError as exc:
        console.say(f"REFUSED: {exc}")
        return EXIT_REFUSED
    assert buyer_env is not None

    cfg = RunConfig(
        api=base,
        agent=args.agent,
        intent=args.intent,
        buyer_secret_env=buyer_env,
        adjudicator_key_env=adjudicator_env,
        evidence_dir=args.evidence_dir,
        dry_run=args.dry_run,
        until=args.until,
        from_task=args.from_task,
        from_dispute=args.from_dispute,
        rpc_url=args.rpc_url,
        horizon_url=args.horizon_url.rstrip("/"),
        **({"dispute_reason": args.dispute_reason} if args.dispute_reason else {}),
    )
    do_sleep = sleep or time.sleep
    retry = RetryPolicy(sleep=do_sleep)
    with httpx.Client(transport=transport, follow_redirects=True) as http:
        runner = Runner(
            cfg=cfg,
            budgets=budgets or Budgets(),
            api=OrizonApi(client=http, base=base, retry=retry),
            http=http,
            console=console,
            log=EvidenceLog(cfg.evidence_dir),
            store=StateStore(cfg.evidence_dir),
            environ=dict(os.environ if environ is None else environ),
            sleep=do_sleep,
            clock=clock or time.monotonic,
            retry=retry,
        )
        return runner.run()
