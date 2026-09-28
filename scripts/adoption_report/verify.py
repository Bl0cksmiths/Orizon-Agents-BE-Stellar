"""Every claim the adoption endpoint makes, re-checked against the chain.

The API is treated as a witness, not an authority. For each claim:

  * **Owner** — the account is a real testnet account (Horizon), it is not in
    the committed team register, and for each of its agents
    `AgentRegistry.owner_of(agent_id)` returns exactly that account.
  * **Agent** — the registry's own `active` flag matches the claim. `bound` is
    an off-chain fact (the binding store), so it is reported as the API's word
    and never counted as verified.
  * **Settled workflow** — the transaction exists and is SUCCESSFUL, it invokes
    the configured escrow, and the escrow's `charged` event names that agent
    for the claimed amount and job. Past the RPC's ~7-day event window, where
    Horizon still has the envelope but no events, the proof is the SUCCESSFUL
    `settle` invocation's own `payouts` naming the agent and amount (escrow v2
    emits one `charged` per payout, and a failed payout reverts the whole
    settle), and the report says which proof was used. The payer is read back
    from `PaymentEscrow.authorization(auth_id)`, and a workflow the agent's own
    owner paid for is self-settlement, not adoption.

Then the three SOW §6.3 totals are recounted from ONLY the facts that verified
and compared with what the API says, total by total and MET flag by MET flag.

A `Check` is `ok=True` (verified), `ok=False` (failed) or `ok=None` (reported,
not verifiable — never counted as a pass or a failure). A failure is either
`contradicted` (the chain answered, and the answer is not the claim) or
`unreadable` (the chain could not be read), so an RPC outage is never reported
as a forgery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from stellar_sdk import StrKey

from .api import AdoptionClaim, AgentClaim, ExclusionClaim, OperatorClaim, WorkflowClaim
from .chain import ChainReader, RpcError, SimulationError, TxRecord
from .config import EXPLORER_ACCOUNT, EXPLORER_TX, TARGETS, usdc_to_stroops
from .register import TeamRegister
from .retry import RetryableStatus

READ_ERRORS = (httpx.HTTPError, RetryableStatus, RpcError, ValueError)

CONTRADICTED = "contradicted"
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Check:
    subject: str
    name: str
    ok: bool | None
    detail: str
    kind: str = CONTRADICTED  # meaningful only when ok is False


def _short(value: str) -> str:
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else value


@dataclass
class VerifiedWorkflow:
    claim: WorkflowClaim
    agent_id: str
    tx: TxRecord | None = None
    proof: str = ""
    checks: list[Check] = field(default_factory=list)
    counted: bool = False


@dataclass
class VerifiedAgent:
    claim: AgentClaim
    owner: str
    checks: list[Check] = field(default_factory=list)
    workflows: list[VerifiedWorkflow] = field(default_factory=list)
    counted: bool = False


@dataclass
class VerifiedOperator:
    claim: OperatorClaim
    checks: list[Check] = field(default_factory=list)
    agents: list[VerifiedAgent] = field(default_factory=list)
    counted: bool = False


@dataclass
class Verification:
    operators: list[VerifiedOperator]
    excluded: list[ExclusionClaim]
    global_checks: list[Check]
    recount: dict[str, int]
    count_checks: list[Check]

    def all_checks(self) -> list[Check]:
        out = list(self.global_checks)
        for op in self.operators:
            out.extend(op.checks)
            for agent in op.agents:
                out.extend(agent.checks)
                for wf in agent.workflows:
                    out.extend(wf.checks)
        return out

    def claim_failures(self) -> list[Check]:
        return [c for c in self.all_checks() if c.ok is False]

    def count_failures(self) -> list[Check]:
        return [c for c in self.count_checks if c.ok is False]


def _passed(checks: list[Check]) -> bool:
    return all(c.ok is not False for c in checks)


# ── workflows ───────────────────────────────────────────────────
def _charged_proof(
    tx: TxRecord, escrow: str, agent_id: str, claim: WorkflowClaim, subject: str
) -> tuple[Check, str | None, str]:
    """(the check, the authorization id it names, which proof was used)."""
    want_amount = usdc_to_stroops(claim.amount_usdc)
    want_job = claim.job_id_hex.lower()
    if tx.events is not None:
        charged = [e for e in tx.events if e.contract_id == escrow and e.topics and e.topics[0] == "charged"]
        named = [e for e in charged if len(e.topics) > 1 and e.topics[1] == agent_id]
        if not named:
            others = sorted({str(e.topics[1]) for e in charged if len(e.topics) > 1})
            return (
                Check(
                    subject,
                    "charged_names_agent",
                    False,
                    f"tx {tx.tx_hash} emitted {len(charged)} charged event(s) from the escrow, naming {others}; "
                    f"none names agent {agent_id}",
                ),
                None,
                "",
            )
        for e in named:
            data = e.value if isinstance(e.value, list) else []
            if len(data) > 3 and int(data[2]) == want_amount and str(data[3]).lower() == want_job:
                return (
                    Check(
                        subject,
                        "charged_names_agent",
                        True,
                        f"charged({agent_id}) for {want_amount} stroops, job {want_job} (RPC contract events)",
                    ),
                    str(data[1]),
                    "charged event",
                )
        seen = [(int(d[2]), str(d[3])) for d in (e.value for e in named) if isinstance(d, list) and len(d) > 3]
        return (
            Check(
                subject,
                "charged_names_agent",
                False,
                f"charged event(s) for {agent_id} in tx {tx.tx_hash} carry (stroops, job) {seen}; "
                f"the API claims ({want_amount}, {want_job})",
            ),
            None,
            "",
        )

    # Horizon answered: no events. The settle invocation's own arguments.
    for call in tx.invocations:
        if call.contract_id != escrow or call.function != "settle" or len(call.args) < 4:
            continue
        job = str(call.args[2]).lower()
        payouts = call.args[3] if isinstance(call.args[3], list) else []
        for p in payouts:
            if (
                isinstance(p, dict)
                and p.get("agent_id") == agent_id
                and int(p.get("amount") or 0) == want_amount
                and job == want_job
            ):
                return (
                    Check(
                        subject,
                        "charged_names_agent",
                        True,
                        f"tx is past the RPC's event window; the SUCCESSFUL settle's payouts name {agent_id} "
                        f"for {want_amount} stroops, job {job} (Horizon envelope)",
                    ),
                    str(call.args[1]),
                    "settle payout (events past RPC retention)",
                )
    functions = sorted({f"{_short(c.contract_id)}.{c.function}" for c in tx.invocations})
    return (
        Check(
            subject,
            "charged_names_agent",
            False,
            f"tx {tx.tx_hash} is past the RPC's event window and its invocation {functions} has no settle payout "
            f"naming {agent_id} for {want_amount} stroops, job {want_job}; the payment to {agent_id} cannot be shown",
        ),
        None,
        "",
    )


def verify_workflow(
    reader: ChainReader, escrow: str, agent_id: str, owner: str, claim: WorkflowClaim
) -> VerifiedWorkflow:
    subject = f"workflow {_short(claim.tx_hash)} ({agent_id})"
    out = VerifiedWorkflow(claim=claim, agent_id=agent_id)
    expected_link = EXPLORER_TX.format(claim.tx_hash)
    out.checks.append(
        Check(
            subject,
            "explorer_link",
            claim.explorer.rstrip("/") == expected_link,
            f"API links {claim.explorer!r}; the testnet link is {expected_link}",
        )
    )
    try:
        tx = reader.transaction(claim.tx_hash)
    except READ_ERRORS as exc:
        out.checks.append(Check(subject, "tx_success", False, f"could not read tx {claim.tx_hash}: {exc}", UNREADABLE))
        return out
    out.tx = tx
    if tx.status != "SUCCESS":
        detail = (
            f"tx {claim.tx_hash} is not on testnet (RPC and Horizon both answer NOT_FOUND)"
            if tx.status == "NOT_FOUND"
            else f"tx {claim.tx_hash} is {tx.status} on ledger {tx.ledger} (read from {tx.source})"
        )
        out.checks.append(Check(subject, "tx_success", False, detail))
        return out
    out.checks.append(Check(subject, "tx_success", True, f"SUCCESS on ledger {tx.ledger} (read from {tx.source})"))

    on_escrow = [c for c in tx.invocations if c.contract_id == escrow]
    if not on_escrow:
        called = sorted({f"{c.contract_id}.{c.function}" for c in tx.invocations}) or ["no contract"]
        out.checks.append(
            Check(subject, "invokes_escrow", False, f"tx {claim.tx_hash} invokes {called}, not the escrow {escrow}")
        )
        return out
    out.checks.append(
        Check(subject, "invokes_escrow", True, f"invokes {escrow}.{', '.join(c.function for c in on_escrow)}")
    )

    charged, auth_id, proof = _charged_proof(tx, escrow, agent_id, claim, subject)
    out.checks.append(charged)
    out.proof = proof
    if charged.ok is not True:
        return out

    payer = claim.payer
    try:
        view = reader.authorization(escrow, auth_id) if auth_id else None
    except READ_ERRORS as exc:
        view = None
        out.checks.append(Check(subject, "payer", False, f"could not read authorization({auth_id}): {exc}", UNREADABLE))
    else:
        if view is None:
            out.checks.append(
                Check(
                    subject,
                    "payer",
                    None,
                    f"authorization({auth_id}) is no longer readable on the escrow; the payer is the API's word",
                )
            )
        elif view.get("payer") != claim.payer:
            out.checks.append(
                Check(
                    subject,
                    "payer",
                    False,
                    f"authorization({auth_id}).payer is {view.get('payer')}, not the claimed payer {claim.payer}",
                )
            )
        else:
            payer = str(view["payer"])
            out.checks.append(Check(subject, "payer", True, f"authorization({auth_id}).payer is {payer}"))
    if payer == owner:
        out.checks.append(
            Check(
                subject,
                "not_self_settled",
                False,
                f"self-settled: the payer {payer} is the agent's own owner, so this is not an external workflow",
            )
        )
    return out


# ── agents and operators ────────────────────────────────────────
def verify_agent(reader: ChainReader, registry: str, escrow: str, owner: str, claim: AgentClaim) -> VerifiedAgent:
    subject = f"agent {claim.agent_id}"
    out = VerifiedAgent(claim=claim, owner=owner)
    try:
        actual = reader.owner_of(registry, claim.agent_id)
    except SimulationError as exc:
        out.checks.append(
            Check(
                subject,
                "owner_of",
                False,
                f"AgentRegistry.owner_of({claim.agent_id}) failed: {exc}; the registry holds no such agent",
            )
        )
    except READ_ERRORS as exc:
        out.checks.append(
            Check(subject, "owner_of", False, f"could not read owner_of({claim.agent_id}): {exc}", UNREADABLE)
        )
    else:
        out.checks.append(
            Check(
                subject,
                "owner_of",
                actual == owner,
                f"AgentRegistry.owner_of({claim.agent_id}) is {actual}"
                + ("" if actual == owner else f", not the claimed owner {owner}"),
            )
        )
    try:
        record = reader.registry_record(registry, claim.agent_id)
    except SimulationError as exc:
        out.checks.append(Check(subject, "active", False, f"AgentRegistry.get({claim.agent_id}) failed: {exc}"))
    except READ_ERRORS as exc:
        out.checks.append(Check(subject, "active", False, f"could not read get({claim.agent_id}): {exc}", UNREADABLE))
    else:
        actual_active = None if record is None else record.get("active")
        out.checks.append(
            Check(
                subject,
                "active",
                actual_active == claim.active,
                f"AgentRegistry.get({claim.agent_id}).active is {actual_active}; the API claims {claim.active}",
            )
        )
    out.checks.append(
        Check(subject, "bound", None, f"bound={claim.bound} is an off-chain binding: the API's word, not verified")
    )
    for wf in claim.settled_workflows:
        out.workflows.append(verify_workflow(reader, escrow, claim.agent_id, owner, wf))
    return out


def verify_operator(
    reader: ChainReader, registry: str, escrow: str, team: TeamRegister, claim: OperatorClaim
) -> VerifiedOperator:
    subject = f"owner {claim.owner}"
    out = VerifiedOperator(claim=claim)
    if not StrKey.is_valid_ed25519_public_key(claim.owner):
        out.checks.append(Check(subject, "owner_is_account", False, f"{claim.owner!r} is not a Stellar account strkey"))
        return out
    expected_link = EXPLORER_ACCOUNT.format(claim.owner)
    out.checks.append(
        Check(
            subject,
            "explorer_link",
            claim.owner_explorer.rstrip("/") == expected_link,
            f"API links {claim.owner_explorer!r}; the testnet link is {expected_link}",
        )
    )
    try:
        exists = reader.account_exists(claim.owner)
    except READ_ERRORS as exc:
        out.checks.append(Check(subject, "owner_exists", False, f"could not read the account: {exc}", UNREADABLE))
    else:
        out.checks.append(
            Check(
                subject,
                "owner_exists",
                exists,
                "the account exists on testnet Horizon" if exists else "no such account on testnet Horizon",
            )
        )
    in_team = claim.owner in team.accounts
    out.checks.append(
        Check(
            subject,
            "not_team_wallet",
            not in_team,
            f"a team wallet: {claim.owner} is in the team register {team.path}, so it cannot count as external"
            if in_team
            else f"not in the team register {team.path}",
        )
    )
    for agent in claim.agents:
        out.agents.append(verify_agent(reader, registry, escrow, claim.owner, agent))
    return out


# ── the whole answer ────────────────────────────────────────────
def _global_checks(claim: AdoptionClaim, team: TeamRegister) -> list[Check]:
    checks: list[Check] = []
    for metric, target in TARGETS.items():
        api_target = claim.targets[metric]
        checks.append(
            Check(
                "targets",
                f"target:{metric}",
                api_target == target,
                f"API target {api_target}; SOW §6.3 target {target}",
            )
        )
    if claim.unreadable_agents and not claim.degraded:
        checks.append(
            Check(
                "degraded",
                "degraded_flag",
                False,
                f"the API lists {len(claim.unreadable_agents)} unreadable agent(s) but says degraded=false",
            )
        )
    owners = [op.owner for op in claim.operators]
    for owner in sorted({o for o in owners if owners.count(o) > 1}):
        checks.append(Check(f"owner {owner}", "listed_once", False, "the owner is listed as two operators"))
    agent_ids = [a.agent_id for op in claim.operators for a in op.agents]
    for agent_id in sorted({a for a in agent_ids if agent_ids.count(a) > 1}):
        checks.append(Check(f"agent {agent_id}", "listed_once", False, "the agent is listed more than once"))
    excluded_owners = {ex.owner for ex in claim.excluded}
    excluded_agents = {a for ex in claim.excluded for a in ex.agent_ids}
    for owner in sorted(set(owners) & excluded_owners):
        checks.append(Check(f"owner {owner}", "not_excluded", False, "claimed as external AND listed as excluded"))
    for agent_id in sorted(set(agent_ids) & excluded_agents):
        checks.append(Check(f"agent {agent_id}", "not_excluded", False, "claimed as external AND listed as excluded"))
    for ex in claim.excluded:
        if ex.reason == "team_wallet" and ex.owner not in team.accounts:
            checks.append(
                Check(
                    f"owner {ex.owner}",
                    "exclusion_in_register",
                    None,
                    f"excluded as a team wallet, but the register {team.path} does not name it",
                )
            )
    return checks


def recount(operators: list[VerifiedOperator]) -> dict[str, int]:
    """The three totals, from ONLY the facts that verified.

    An agent counts when its owner verified (exists, not a team wallet) and
    `owner_of` returned that owner. An operator wallet counts when at least one
    of its agents counts. A workflow counts — once per distinct job id — when
    every check on it held and its agent counts.
    """
    agents: set[str] = set()
    wallets: set[str] = set()
    jobs: set[str] = set()
    for op in operators:
        owner_ok = _passed(op.checks)
        for agent in op.agents:
            agent_ok = owner_ok and all(c.ok is not False for c in agent.checks if c.name == "owner_of")
            agent.counted = agent_ok
            if not agent_ok:
                continue
            agents.add(agent.claim.agent_id)
            wallets.add(op.claim.owner)
            op.counted = True
            for wf in agent.workflows:
                wf.counted = _passed(wf.checks)
                if wf.counted:
                    jobs.add(wf.claim.job_id_hex.lower())
    return {
        "external_agents": len(agents),
        "unique_operator_wallets": len(wallets),
        "settled_external_workflows": len(jobs),
    }


def met(totals: dict[str, int]) -> dict[str, bool]:
    return {metric: totals[metric] >= target for metric, target in TARGETS.items()}


def compare(claim: AdoptionClaim, counted: dict[str, int]) -> list[Check]:
    checks: list[Check] = []
    recount_met = met(counted)
    for metric in TARGETS:
        checks.append(
            Check(
                "totals",
                f"total:{metric}",
                claim.totals[metric] == counted[metric],
                f"API says {claim.totals[metric]}, the verified recount is {counted[metric]}",
            )
        )
        verdict = "MET" if recount_met[metric] else "NOT MET"
        checks.append(
            Check(
                "met",
                f"met:{metric}",
                claim.met[metric] == recount_met[metric],
                f"API says met={claim.met[metric]}; the recount ({counted[metric]} of {TARGETS[metric]}) is {verdict}",
            )
        )
    return checks


def verify(
    claim: AdoptionClaim, reader: ChainReader, *, registry: str, escrow: str, team: TeamRegister
) -> Verification:
    operators = [verify_operator(reader, registry, escrow, team, op) for op in claim.operators]
    counted = recount(operators)
    return Verification(
        operators=operators,
        excluded=claim.excluded,
        global_checks=_global_checks(claim, team),
        recount=counted,
        count_checks=compare(claim, counted),
    )


def summary_line(check: Check) -> str:
    return f"{check.subject} — {check.name}: {check.detail}"


def as_dict(check: Check) -> dict[str, Any]:
    return {
        "subject": check.subject,
        "check": check.name,
        "ok": check.ok,
        "kind": check.kind if check.ok is False else None,
        "detail": check.detail,
    }
