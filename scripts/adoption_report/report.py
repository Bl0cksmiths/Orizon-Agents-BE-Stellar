"""The evidence the 5.05 index is built from: one Markdown table set, one JSON document.

Both are rendered from the VERIFIED facts, never from the API's answer as
given: every count is the recount, every link is built here from the hash or
address the chain confirmed, and every target line says MET or NOT MET with
nothing in between. A NOT MET is printed as such.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import AdoptionClaim
from .config import (
    EXPLORER_ACCOUNT,
    EXPLORER_CONTRACT,
    EXPLORER_TX,
    TARGET_LABELS,
    TARGETS,
    usdc_to_stroops,
)
from .register import TeamRegister
from .verify import Check, Verification, as_dict, met, summary_line

MARKDOWN_NAME = "adoption-report.md"
JSON_NAME = "adoption-report.json"
SCHEMA = "orizon.adoption-report/1"


@dataclass(frozen=True)
class RunFacts:
    """Where the run looked, so a reader can repeat every read."""

    api_url: str
    rpc_url: str
    horizon_url: str
    escrow: str
    registry: str
    generated_utc: str
    exit_code: int
    verdict: str


def _cell(value: Any) -> str:
    """A table cell: on-chain names are untrusted text, so no pipes or line breaks."""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _acct(address: str) -> str:
    return f"[`{address[:6]}…{address[-4:]}`]({EXPLORER_ACCOUNT.format(address)})"


def _tx(tx_hash: str) -> str:
    return f"[`{tx_hash[:10]}…`]({EXPLORER_TX.format(tx_hash)})"


def _mark(check: Check) -> str:
    return {True: "verified", False: "FAILED", None: "not verifiable"}[check.ok]


def target_lines(totals: dict[str, int]) -> list[str]:
    flags = met(totals)
    return [
        f"- **{TARGET_LABELS[m]}: {totals[m]} of {TARGETS[m]} — {'MET' if flags[m] else 'NOT MET'}**" for m in TARGETS
    ]


def render_markdown(claim: AdoptionClaim, result: Verification, team: TeamRegister, run: RunFacts) -> str:
    lines = [
        "# Adoption evidence — SOW §6.3 (testnet)",
        "",
        f"Generated {run.generated_utc} by `python -m scripts.adoption_report`, re-verifying "
        f"`{run.api_url}` against the chain. Nothing below is the API's word unless it says so.",
        "",
        f"- Network: **testnet** (RPC `{run.rpc_url}`, Horizon `{run.horizon_url}`)",
        f"- AgentRegistry: [`{run.registry}`]({EXPLORER_CONTRACT.format(run.registry)})",
        f"- PaymentEscrow: [`{run.escrow}`]({EXPLORER_CONTRACT.format(run.escrow)})",
        f"- Team register: `{team.path}` (sha256 `{team.sha256[:16]}…`, {len(team.accounts)} account(s))",
        "",
        "## Targets",
        "",
        *target_lines(result.recount),
        "",
        f"**Verification: {run.verdict}** (exit {run.exit_code}).",
    ]
    if claim.degraded:
        lines += [
            "",
            f"> The API reported itself DEGRADED: {len(claim.unreadable_agents)} agent(s) could not be read "
            f"({', '.join(_cell(a) for a in claim.unreadable_agents) or 'none named'}). Agents it could not read "
            "are not in these totals.",
        ]
    failures = result.claim_failures() + result.count_failures()
    if failures:
        lines += ["", "## Failed checks", ""]
        lines += [f"- {_cell(summary_line(c))}" for c in failures]

    lines += [
        "",
        "## External operators and agents",
        "",
        "| Operator | Owner wallet | Agent | Name | Active | Bound (API's word) | Counted |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, op in enumerate(result.operators, 1):
        if not op.agents:
            lines.append(f"| OP-{i} | {_acct(op.claim.owner)} | — | — | — | — | no |")
        for agent in op.agents:
            lines.append(
                f"| OP-{i} | {_acct(op.claim.owner)} | `{_cell(agent.claim.agent_id)}` | {_cell(agent.claim.name)} "
                f"| {agent.claim.active} | {agent.claim.bound} | {'yes' if agent.counted else 'no'} |"
            )

    lines += [
        "",
        "## Settled workflows",
        "",
        "| Agent | Job | Transaction | Ledger | Amount (stroops) | Payer | Proof | Counted |",
        "|---|---|---|---|---|---|---|---|",
    ]
    any_wf = False
    for op in result.operators:
        for agent in op.agents:
            for wf in agent.workflows:
                any_wf = True
                ledger = wf.tx.ledger if wf.tx is not None else "—"
                lines.append(
                    f"| `{_cell(agent.claim.agent_id)}` | `{_cell(wf.claim.job_id_hex)}` | {_tx(wf.claim.tx_hash)} "
                    f"| {ledger} | {usdc_to_stroops(wf.claim.amount_usdc)} | {_acct(wf.claim.payer)} "
                    f"| {_cell(wf.proof or '—')} | {'yes' if wf.counted else 'no'} |"
                )
    if not any_wf:
        lines.append("| — | — | none claimed | — | — | — | — | — |")

    lines += [
        "",
        "## Excluded (wallets the Blocksmiths control)",
        "",
        "| Owner | Reason | Role | Agents |",
        "|---|---|---|---|",
    ]
    for ex in result.excluded:
        lines.append(
            f"| {_acct(ex.owner)} | {ex.reason} | {_cell(ex.role)} | {_cell(', '.join(ex.agent_ids) or '—')} |"
        )
    if not result.excluded:
        lines.append("| — | — | — | none listed |")

    lines += ["", "## Every check", "", "| Subject | Check | Result | Detail |", "|---|---|---|---|"]
    for c in result.all_checks() + result.count_checks:
        lines.append(f"| {_cell(c.subject)} | {c.name} | {_mark(c)} | {_cell(c.detail)} |")
    return "\n".join(lines) + "\n"


def render_json(claim: AdoptionClaim, result: Verification, team: TeamRegister, run: RunFacts) -> dict[str, Any]:
    operators = []
    for op in result.operators:
        agents = []
        for agent in op.agents:
            agents.append(
                {
                    "agent_id": agent.claim.agent_id,
                    "name": agent.claim.name,
                    "active": agent.claim.active,
                    "bound_api_claim": agent.claim.bound,
                    "counted": agent.counted,
                    "settled_workflows": [
                        {
                            "job_id_hex": wf.claim.job_id_hex,
                            "tx_hash": wf.claim.tx_hash,
                            "explorer": EXPLORER_TX.format(wf.claim.tx_hash),
                            "ledger": wf.tx.ledger if wf.tx is not None else None,
                            "read_from": wf.tx.source if wf.tx is not None else None,
                            "amount_stroops": usdc_to_stroops(wf.claim.amount_usdc),
                            "payer": wf.claim.payer,
                            "proof": wf.proof or None,
                            "counted": wf.counted,
                        }
                        for wf in agent.workflows
                    ],
                }
            )
        operators.append(
            {
                "owner": op.claim.owner,
                "owner_explorer": EXPLORER_ACCOUNT.format(op.claim.owner),
                "counted": op.counted,
                "agents": agents,
            }
        )
    recount_met = met(result.recount)
    return {
        "schema": SCHEMA,
        "generated_utc": run.generated_utc,
        "network": "testnet",
        "verdict": run.verdict,
        "exit_code": run.exit_code,
        "sources": {
            "api": run.api_url,
            "api_generated_at": claim.generated_at,
            "rpc": run.rpc_url,
            "horizon": run.horizon_url,
        },
        "contracts": {
            "agent_registry": {"id": run.registry, "explorer": EXPLORER_CONTRACT.format(run.registry)},
            "payment_escrow": {"id": run.escrow, "explorer": EXPLORER_CONTRACT.format(run.escrow)},
        },
        "team_register": {"path": str(team.path), "sha256": team.sha256, "accounts": len(team.accounts)},
        "targets": dict(TARGETS),
        "totals": dict(result.recount),
        "api_totals": dict(claim.totals),
        "met": recount_met,
        "met_lines": {m: ("MET" if recount_met[m] else "NOT MET") for m in TARGETS},
        "degraded": claim.degraded,
        "unreadable_agents": claim.unreadable_agents,
        "operators": operators,
        "excluded": [
            {
                "owner": ex.owner,
                "owner_explorer": EXPLORER_ACCOUNT.format(ex.owner),
                "reason": ex.reason,
                "role": ex.role,
                "agent_ids": ex.agent_ids,
            }
            for ex in result.excluded
        ],
        "checks": [as_dict(c) for c in result.all_checks() + result.count_checks],
    }


def write(out_dir: Path, markdown: str, document: dict[str, Any]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / MARKDOWN_NAME
    json_path = out_dir / JSON_NAME
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return md_path, json_path
