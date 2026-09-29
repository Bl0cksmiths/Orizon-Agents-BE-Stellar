"""Every GO / NO-GO check, in the order the recording depends on them.

Each check reads, judges and says what to fix. It is one of

  PASS     holds; nothing to do
  WARN     holds, with a caveat the take must act on (a team wallet to disclose
           on camera)
  FAIL     does not hold; `fix` says exactly what to change
  SKIPPED  could not be judged — a flag was not given, or a check it depends
           on failed. SKIPPED is NEVER a pass: a required check that was
           skipped keeps the verdict at NO-GO.

A check is `required` when a take without it would be wasted; the verdict is
GO only when every required check is PASS or WARN. Advisory checks are shown
and never gate.

Where a fact comes from, and why it is the honest source:

  network     RPC `getNetwork`, Horizon `/` and `GET /api/stellar/network` —
              all three must say testnet, or the run is REFUSED outright.
  build       the routes themselves: a build that predates 5.02 answers 404.
              `/readiness` is root-level and orizons.xyz proxies only `/api`,
              so it is read from the backend host (`--backend`).
  escrow      `PaymentEscrow.version()` and `settler()` by simulation, against
              the escrow `/api/stellar/network` names; the settler is compared
              with `/readiness` `ratings.signer`, the key the deployment signs
              settlements with.
  refunds     `/readiness` `disputes.reconcile.enabled`, which the backend
              computes as REFUND_RECONCILE_ENABLED AND DISPUTE_REFUNDS_ENABLED
              (`app/services/refund_reconcile.status`) — the only public field
              that can say the refund switch is on. The settler's balance is a
              Horizon read.
  operator    `GET /api/ecosystem/adoption` for who is external, and each
              external agent's `GET /api/agents/{id}/readiness`; the
              reference agent's own `GET /` (`--operator-endpoint`) for its
              fault-injection field and header.
  exclusion   `GET /api/stellar/reputation` (+ `/params` for the floor and the
              switch), joined with `GET /api/agents` for registered, listed
              agents. A `degraded` read is the prior served because the ledger
              could not be read, and a `stale` one is an old read: neither is
              a verdict on the agent, so neither ever counts. When every
              agent reads degraded (a cold start), the batch is read again.
              The same batch, with each row's status and binding, counts the
              routable agents that clear the floor, and the excluded bound is
              judged by the figure the plan card prints (`card_stars`).
  wallets     Horizon, against `--cap` and the fee allowances in `config.py`,
              and the committed team register.
  frontend    a GET of each page the script visits, redirects NOT followed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .api import Answer, Reads, Unreachable
from .chain import READ_ERRORS, ChainReader, RpcError, SimulationError
from .config import (
    BUYER_FEE_ALLOWANCE,
    EXPLORER_ACCOUNT,
    FRONTEND_PAGES,
    OPERATOR_FEE_ALLOWANCE,
    REPUTATION_READ_ATTEMPTS,
    REPUTATION_REREAD_SECONDS,
    SETTLER_FEE_ALLOWANCE,
    TESTNET_PASSPHRASE,
    WARMUP_BUDGET_SECONDS,
    RunConfig,
)
from .register import TeamRegister

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

CHAIN_ERRORS: tuple[type[Exception], ...] = (*READ_ERRORS, RpcError, SimulationError)

# `app/services/operator_readiness.py` READY_KEYS, in its STEP_KEYS order: the
# steps that make an agent routable and dispatchable.
READY_STEPS: tuple[str, ...] = ("registered", "active", "bound", "reachable", "routable")

# `app/services/orchestrator_svc.py` _MIN_ROUTABLE_AGENTS: when fewer routable
# agents than this clear the floor, the planner's starvation backstop
# re-admits sub-floor agents, and the card reads "kept below floor" rather
# than "excluded".
MIN_ROUTABLE_AGENTS = 3

# The escrow version the demo needs, and the defect v1 carries.
ESCROW_V2 = 2
D039 = (
    "D-039: v1's PaymentEscrow.charge cannot move the payer's funds, so no workflow ever settles on it "
    "(Smart-Contract #3)"
)


class Refused(Exception):
    """The run must not be judged at all: the wrong network, or one that cannot be confirmed."""


@dataclass
class Check:
    id: str
    group: str
    title: str
    required: bool = True
    status: str = SKIPPED
    detail: str = ""
    fix: str = ""

    def passed(self, detail: str) -> Check:
        self.status, self.detail, self.fix = PASS, detail, ""
        return self

    def failed(self, detail: str, fix: str) -> Check:
        self.status, self.detail, self.fix = FAIL, detail, fix
        return self

    def warned(self, detail: str, fix: str) -> Check:
        self.status, self.detail, self.fix = WARN, detail, fix
        return self

    def skipped(self, detail: str, fix: str) -> Check:
        self.status, self.detail, self.fix = SKIPPED, detail, fix
        return self


@dataclass
class Facts:
    """What the reads found, shared between the checks that judge it."""

    cold_start_seconds: float | None = None
    warm: bool = False
    network: dict[str, Any] = field(default_factory=dict)
    readiness: dict[str, Any] | None = None
    adoption: dict[str, Any] | None = None
    agents: list[dict[str, Any]] | None = None
    escrow_version: int | None = None
    settler: str | None = None
    below_floor: list[str] = field(default_factory=list)
    floor_bps: int | None = None
    reputations: dict[str, Any] | None = None  # the reputation batch the floor was judged on
    lower_bounds: dict[str, int] = field(default_factory=dict)  # agent id -> lower_bound_bps, as last read
    disclosures: list[str] = field(default_factory=list)

    @property
    def escrow(self) -> str | None:
        contracts = self.network.get("contracts")
        value = contracts.get("payment_escrow") if isinstance(contracts, dict) else None
        return value if isinstance(value, str) and value else None

    @property
    def signer(self) -> str | None:
        ratings = (self.readiness or {}).get("ratings")
        value = ratings.get("signer") if isinstance(ratings, dict) else None
        return value if isinstance(value, str) and value else None


def _short(account: str) -> str:
    return f"{account[:6]}…{account[-4:]}"


def _status_line(answer: Answer) -> str:
    error = answer.obj().get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return f"HTTP {answer.status}" + (f" ({code})" if code else "")


def external_agents(adoption: dict[str, Any] | None) -> list[tuple[str, str]]:
    """(agent_id, owner) for every externally operated agent the adoption endpoint lists."""
    out: list[tuple[str, str]] = []
    for op in (adoption or {}).get("operators") or []:
        if not isinstance(op, dict):
            continue
        for agent in op.get("agents") or []:
            if isinstance(agent, dict) and isinstance(agent.get("agent_id"), str):
                out.append((agent["agent_id"], str(op.get("owner"))))
    return out


def first_unready_step(body: dict[str, Any]) -> dict[str, Any] | None:
    """The first routability step that is not `done`, in the order an operator fixes them."""
    steps = {s.get("key"): s for s in body.get("steps") or [] if isinstance(s, dict)}
    for key in READY_STEPS:
        step = steps.get(key)
        if step is None:
            return {"key": key, "status": "missing", "detail": "the readiness answer has no such step"}
        if step.get("status") != "done":
            return step
    return None


def card_stars(bps: int) -> str:
    """The figure the plan card prints for `bps`: the frontend's `(bps / 2000).toFixed(2)`.

    `scoreOutOfFive` in `lib/reputation-math.ts`, which the exclusions panel
    renders the lower bound and the floor with. `toFixed` rounds the exact
    binary value of the quotient half up, and so does this: `Decimal(float)`
    is that exact value.
    """
    return str(Decimal(bps / 2000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def highest_visibly_below(floor_bps: int) -> int:
    """The highest lower bound the card prints as a smaller figure than the floor's own."""
    floor_figure = Decimal(card_stars(floor_bps))
    bps = floor_bps - 1
    while Decimal(card_stars(bps)) >= floor_figure:
        bps -= 1
    return bps


# ── network ─────────────────────────────────────────────────────
def refuse_unless_testnet(chain: ChainReader) -> None:
    try:
        rpc = chain.rpc_passphrase()
        horizon = chain.horizon_passphrase()
    except CHAIN_ERRORS as exc:
        raise Refused(f"could not confirm the network (a refusal, not a guess): {exc}") from exc
    if rpc != TESTNET_PASSPHRASE:
        raise Refused(f"the RPC's network is {rpc!r}; the demo is recorded on testnet only ({TESTNET_PASSPHRASE!r})")
    if horizon != TESTNET_PASSPHRASE:
        raise Refused(
            f"Horizon's network is {horizon!r}; the demo is recorded on testnet only ({TESTNET_PASSPHRASE!r})"
        )


def check_warm(reads: Reads, facts: Facts, budget: float = WARMUP_BUDGET_SECONDS) -> Check:
    check = Check("network.warm", "network", "The backend is awake (GET /api/health)")
    warm = reads.warm(budget)
    facts.cold_start_seconds = round(warm.seconds, 1)
    facts.warm = warm.ok
    if warm.ok:
        kind = "cold start" if warm.attempts > 1 or warm.seconds >= 5 else "already warm"
        return check.passed(f"answered 200 after {warm.seconds:.1f} s ({kind}, {warm.attempts} probe(s))")
    return check.failed(
        f"no 200 from {reads.api_url('/health')} within {budget:.0f} s "
        f"({warm.attempts} probes; last: {warm.last_error})",
        "Open the Render dashboard for the backend: confirm the service is deployed, not suspended, and that its "
        "last deploy succeeded; then rerun. Every check that reads the API is skipped until it answers.",
    )


def check_api_network(reads: Reads, facts: Facts) -> Check:
    check = Check("network.api", "network", "The API is on testnet (GET /api/stellar/network)")
    if not facts.warm:
        return check.skipped("the backend never answered", "Fix network.warm first.")
    try:
        answer = reads.network()
    except Unreachable as exc:
        return check.failed(str(exc), "Rerun once the backend answers /api/health.")
    if not answer.ok:
        return check.failed(
            f"{answer.url} answered {_status_line(answer)}", "Deploy a backend build that serves /api/stellar/network."
        )
    body = answer.obj()
    if body.get("network_passphrase") != TESTNET_PASSPHRASE:
        raise Refused(
            f"the API at {reads.api} is on {body.get('network')!r} ({body.get('network_passphrase')!r}); "
            "the demo is recorded on testnet only"
        )
    facts.network = body
    return check.passed(f"testnet; PaymentEscrow {facts.escrow}")


# ── the deployed build ──────────────────────────────────────────
def check_adoption_route(reads: Reads, facts: Facts) -> Check:
    check = Check("build.adoption", "build", "The 5.02 adoption route answers (GET /api/ecosystem/adoption)")
    if not facts.warm:
        return check.skipped("the backend never answered", "Fix network.warm first.")
    try:
        answer = reads.adoption()
    except Unreachable as exc:
        return check.failed(str(exc), "Rerun; if it persists, read the backend's log for the adoption route.")
    body = answer.obj()
    if answer.status == 404:
        return check.failed(
            f"{answer.url} answered 404: the deployed backend predates story 5.02",
            "Deploy the backend's main (5.02 merged) to the Render service behind --api.",
        )
    missing = [k for k in ("network", "operators", "totals", "excluded") if k not in body]
    if not answer.ok or missing:
        return check.failed(
            f"{answer.url} answered {_status_line(answer)}" + (f", missing {', '.join(missing)}" if missing else ""),
            "Deploy a backend build whose GET /api/ecosystem/adoption answers the frozen 5.02 shape.",
        )
    if body.get("network") != "testnet":
        raise Refused(f"GET /api/ecosystem/adoption reports network {body.get('network')!r}; testnet only")
    facts.adoption = body
    return check.passed(f"answered; {len(external_agents(body))} externally operated agent(s)")


def check_agent_readiness_route(reads: Reads, facts: Facts) -> Check:
    check = Check(
        "build.agent_readiness", "build", "The 5.02 operator readiness route answers (GET /api/agents/{id}/readiness)"
    )
    if not facts.warm:
        return check.skipped("the backend never answered", "Fix network.warm first.")
    external = external_agents(facts.adoption)
    listed = [a.get("id") for a in facts.agents or [] if a.get("source") == "onchain"]
    probe = external[0][0] if external else (str(listed[0]) if listed else "demo_preflight_probe")
    try:
        answer = reads.agent_readiness(probe)
    except Unreachable as exc:
        return check.failed(str(exc), "Rerun; if it persists, read the backend's log for the readiness route.")
    body = answer.obj()
    if answer.ok and isinstance(body.get("ready"), bool) and isinstance(body.get("steps"), list):
        return check.passed(f"answered for {probe}")
    if answer.status == 404:
        return check.failed(
            f"{answer.url} answered 404: the deployed backend predates story 5.02",
            "Deploy the backend's main (5.02 merged) to the Render service behind --api.",
        )
    return check.failed(
        f"{answer.url} answered {_status_line(answer)} without `ready` and `steps`",
        "Deploy a backend build whose readiness route answers the 5.02 shape.",
    )


def read_backend_readiness(reads: Reads, facts: Facts) -> Check:
    check = Check("build.readiness", "build", "The backend reports ready, with the 5.01 fields (GET /readiness)")
    try:
        answer = reads.readiness()
    except Unreachable as exc:
        return check.failed(
            str(exc),
            f"Check --backend ({reads.backend}) is the host orizons.xyz proxies /api to, and that it is deployed.",
        )
    body = answer.obj()
    if answer.status not in (200, 503) or not body:
        return check.failed(
            f"{answer.url} answered {_status_line(answer)}",
            f"Pass --backend with the backend's own host; /readiness is root-level and {reads.api} proxies only /api.",
        )
    facts.readiness = body
    missing: list[str] = []
    disputes = body.get("disputes")
    if not isinstance(disputes, dict):
        missing.append("disputes")
    else:
        if "store" not in disputes:
            missing.append("disputes.store")
        reconcile = disputes.get("reconcile")
        if not isinstance(reconcile, dict) or "enabled" not in reconcile:
            missing.append("disputes.reconcile")
    escrow = body.get("escrow")
    if not isinstance(escrow, dict) or "contract" not in escrow or "version" not in escrow:
        missing.append("escrow {contract, version}")
    if missing:
        return check.failed(
            f"{answer.url} has no {', '.join(missing)}: the deployed backend predates story 5.01's readiness fields",
            "Deploy the backend's main (5.01 + 5.02 merged) to the Render service.",
        )
    if body.get("status") != "ready":
        return check.failed(
            f"status {body.get('status')!r} (llm {body.get('llm')!r}, stellar {body.get('stellar')!r})",
            "Set the missing configuration on the Render dashboard: OPENAI_API_KEY for `llm`, the contract ids, "
            "STELLAR_RPC_URL and STELLAR_ADMIN_ADDRESS for `stellar`.",
        )
    reported = escrow.get("contract") if isinstance(escrow, dict) else None
    if facts.escrow and reported != facts.escrow:
        return check.failed(
            f"/readiness names escrow {reported}, but {reads.api}/api/stellar/network names {facts.escrow}: "
            "the two hosts are not the same deployment",
            "Pass --backend with the host that --api's /api proxy forwards to.",
        )
    return check.passed(f"ready; disputes store {disputes.get('store') if isinstance(disputes, dict) else '?'}")


# ── the escrow ──────────────────────────────────────────────────
def check_escrow_version(chain: ChainReader, facts: Facts) -> Check:
    check = Check("escrow.version", "escrow", "PaymentEscrow is v2 (version() == 2)")
    escrow = facts.escrow
    if escrow is None:
        return check.skipped("the API never named its PaymentEscrow", "Fix network.api first.")
    try:
        version = chain.escrow_version(escrow)
    except CHAIN_ERRORS as exc:
        return check.failed(f"version() could not be read: {exc}", "Rerun; the RPC did not answer.")
    facts.escrow_version = version
    if version == ESCROW_V2:
        return check.passed(f"{escrow} answers version() = 2")
    if version == 1:
        return check.failed(
            f"{escrow} has no version(): it is escrow v1. {D039}",
            "Deploy PaymentEscrow v2 and set STELLAR_PAYMENT_ESCROW to its id on the Render dashboard, then redeploy.",
        )
    return check.failed(
        f"{escrow} answers version() = {version}; the demo is built for v2",
        "Point STELLAR_PAYMENT_ESCROW at the v2 escrow the backend was built against.",
    )


def check_escrow_settler(chain: ChainReader, facts: Facts) -> Check:
    check = Check("escrow.settler", "escrow", "The escrow's settler() is the deployment's signing key")
    escrow = facts.escrow
    if escrow is None or facts.escrow_version != ESCROW_V2:
        return check.skipped("the escrow is not a readable v2", "Fix escrow.version first.")
    if facts.readiness is None:
        return check.skipped("/readiness was not read", "Fix build.readiness first.")
    signer = facts.signer
    if signer is None:
        return check.failed(
            "/readiness reports no `ratings.signer`: the deployment has no usable signing key",
            "Set STELLAR_SIGNING_KEY on the Render dashboard to the escrow's settler secret.",
        )
    try:
        settler = chain.escrow_settler(escrow)
    except CHAIN_ERRORS as exc:
        return check.failed(f"settler() could not be read: {exc}", "Rerun; the RPC did not answer.")
    facts.settler = settler
    if settler != signer:
        return check.failed(
            f"PaymentEscrow.settler() is {settler}, but the deployment signs with {signer}: every settle and "
            "refund it submits reverts Unauthorized",
            f"Set STELLAR_SIGNING_KEY to {_short(settler)}'s secret, or deploy an escrow v2 whose settler is {signer}.",
        )
    return check.passed(f"settler() = {settler} = /readiness ratings.signer")


# ── refunds ─────────────────────────────────────────────────────
def check_refunds_enabled(facts: Facts) -> Check:
    check = Check("refunds.enabled", "refunds", "Dispute refunds are switched on (/readiness disputes.reconcile)")
    disputes = (facts.readiness or {}).get("disputes")
    reconcile = disputes.get("reconcile") if isinstance(disputes, dict) else None
    if not isinstance(reconcile, dict) or "enabled" not in reconcile:
        return check.skipped("/readiness has no disputes.reconcile block", "Fix build.readiness first.")
    if reconcile.get("enabled") is not True:
        return check.failed(
            "disputes.reconcile.enabled is false: DISPUTE_REFUNDS_ENABLED or REFUND_RECONCILE_ENABLED is off, "
            "so an uphold answers 503 dispute_refunds_disabled",
            "On the Render dashboard set DISPUTE_REFUNDS_ENABLED=true and REFUND_RECONCILE_ENABLED=true "
            "(API_KEY must be set too, or the service refuses to boot), then redeploy.",
        )
    running = reconcile.get("running")
    return check.passed("refunds and the reconcile sweep are on" + ("" if running else "; the sweep is not running"))


def check_dispute_store(facts: Facts) -> Check:
    # Required, not advisory: story 5.01 AC4 (a dispute accepted after a
    # restart) and the script's refunds row both need DATABASE_URL. Recording
    # "in one sitting" is no workaround — a free-tier sleep is not scheduled.
    check = Check("refunds.store", "refunds", "Settlements and disputes survive a restart (a durable store)")
    disputes = (facts.readiness or {}).get("disputes")
    store = disputes.get("store") if isinstance(disputes, dict) else None
    if store is None:
        return check.skipped("/readiness has no disputes.store", "Fix build.readiness first.")
    if store != "postgres":
        return check.failed(
            f"disputes.store is {store!r}, not a durable store: a restart between the settle and the dispute "
            "(a free-tier sleep) loses the settlement, and the dispute scene with it",
            "Set DATABASE_URL on the Render dashboard to a Postgres database, then redeploy; /readiness must "
            "read disputes.store: postgres.",
        )
    return check.passed("postgres")


def _funded(check: Check, chain: ChainReader, account: str, need: float, what: str) -> Check:
    try:
        facts = chain.account(account)
    except CHAIN_ERRORS as exc:
        return check.failed(f"Horizon could not be read for {account}: {exc}", "Rerun; Horizon did not answer.")
    if facts is None:
        return check.failed(
            f"{account} does not exist on testnet",
            f"Fund it: https://friendbot.stellar.org/?addr={account} ({EXPLORER_ACCOUNT.format(account)}).",
        )
    if facts.spendable < need:
        return check.failed(
            f"{account} can spend {facts.spendable:.7f} XLM above its {facts.minimum_balance:.1f} XLM reserve; "
            f"{what} needs {need:.7f}",
            f"Send at least {need - facts.spendable:.7f} XLM to {account} (friendbot, or a transfer).",
        )
    return check.passed(
        f"{account} can spend {facts.spendable:.7f} XLM above its {facts.minimum_balance:.1f} XLM reserve "
        f"(needs {need:.7f})"
    )


def check_settler_balance(chain: ChainReader, facts: Facts, cfg: RunConfig) -> Check:
    check = Check("refunds.settler_balance", "refunds", "The settler can pay one refund plus fees")
    settler = facts.settler or facts.signer
    if settler is None:
        return check.skipped("no settler address is known", "Fix escrow.settler (or build.readiness) first.")
    if str(facts.network.get("asset") or "native") != "native":
        return check.skipped(
            f"the asset is {facts.network.get('asset')!r}, not native XLM; this check reads the native balance",
            "Check the settler's balance of that asset by hand.",
        )
    need = cfg.max_refund + SETTLER_FEE_ALLOWANCE
    return _funded(check, chain, settler, need, f"MAX_REFUND_USDC {cfg.max_refund} + {SETTLER_FEE_ALLOWANCE} fees")


# ── the external operator ───────────────────────────────────────
def check_external_count(facts: Facts) -> Check:
    check = Check("operator.external", "operator", "At least one externally operated agent (per the adoption route)")
    if facts.adoption is None:
        return check.skipped("the adoption route was not read", "Fix build.adoption first.")
    external = external_agents(facts.adoption)
    if not external:
        return check.failed(
            "GET /api/ecosystem/adoption lists no externally operated agent",
            "Run an onboarding session (docs/operators/onboarding-session-runbook.md): an operator whose wallet is "
            "not in app/data/team_wallets.json registers and binds an agent.",
        )
    return check.passed(", ".join(f"{a} ({_short(o)})" for a, o in external))


def check_external_ready(reads: Reads, facts: Facts) -> Check:
    check = Check("operator.ready", "operator", "Every external agent is ready and reachable")
    external = external_agents(facts.adoption)
    if not external:
        return check.skipped("there is no external agent to check", "Fix operator.external first.")
    # The exclusion scene's subject fails `routable` by design: the floor is
    # meant to exclude it. It is judged by exclusion.below_floor instead.
    subjects = [a for a, _ in external if a in facts.below_floor]
    judged = [(a, o) for a, o in external if a not in facts.below_floor]
    if not judged:
        return check.failed(
            f"the only external agent(s), {', '.join(subjects)}, are below the floor: none can serve the recording",
            "Onboard (or restore) an external agent that clears the floor, beside the one the floor excludes.",
        )
    unready: list[str] = []
    ready: list[str] = []
    for agent_id, _owner in judged:
        try:
            answer = reads.agent_readiness(agent_id)
        except Unreachable as exc:
            unready.append(f"{agent_id}: readiness unreadable ({exc})")
            continue
        body = answer.obj()
        if not answer.ok:
            unready.append(f"{agent_id}: readiness answered {_status_line(answer)}")
            continue
        step = first_unready_step(body)
        if body.get("ready") is True and step is None:
            ready.append(agent_id)
            continue
        if step is None:
            unready.append(f"{agent_id}: ready is {body.get('ready')!r}")
            continue
        action = f" — {step.get('action')}" if step.get("action") else ""
        unready.append(f"{agent_id}: `{step.get('key')}` is {step.get('status')} ({step.get('detail')}){action}")
    if unready:
        return check.failed(
            "not ready: " + "; ".join(unready),
            "Have each operator fix the named step (their /app/operator page shows the same list), "
            "or have them unbind an agent they no longer run.",
        )
    aside = f"; below the floor by design (the exclusion subject): {', '.join(subjects)}" if subjects else ""
    return check.passed("ready and reachable: " + ", ".join(ready) + aside)


# The reference agent's fault-injection markers (Orizon-Agents-Example-Agent-
# Stellar agent.py, "Fault injection"): a `fault_injection` field in its
# `GET /` health check and an `X-Fault-Injection` header on every answer,
# both present only while FAULT_MODE is set.
FAULT_FIELD = "fault_injection"
FAULT_HEADER = "X-Fault-Injection"


def check_operator_endpoint(reads: Reads, cfg: RunConfig) -> Check:
    check = Check(
        "operator.endpoint",
        "operator",
        "The operator's reference agent answers GET / with no fault injection",
    )
    origin = cfg.operator_endpoint
    if origin is None:
        return check.skipped(
            "no --operator-endpoint given",
            "Pass --operator-endpoint https://… (the endpoint the operator wallet binds in S03).",
        )
    try:
        answer = reads.agent_health(origin)
    except Unreachable as exc:
        return check.failed(
            str(exc),
            "Start (or redeploy) the operator's reference agent, wait for it to answer GET /, and rerun.",
        )
    body = answer.obj()
    if not answer.ok or body.get("ok") is not True:
        return check.failed(
            f'{origin}/ answered HTTP {answer.status} without {{"ok": true}}',
            "Point --operator-endpoint at the reference agent the operator binds, and check it is deployed and awake.",
        )
    faults: list[str] = []
    if FAULT_FIELD in body:
        faults.append(f"its health check carries {FAULT_FIELD}: {body.get(FAULT_FIELD)!r}")
    header = answer.headers.get(FAULT_HEADER.lower())
    if header is not None:
        faults.append(f"it answers with the {FAULT_HEADER} header ({header!r})")
    if faults:
        return check.failed(
            f"{origin}/: fault injection is on: " + "; ".join(faults),
            "Unset FAULT_MODE (and FAULT_SCOPE) on the operator's reference agent and redeploy it. Fault injection "
            "belongs only on the separate faulty test agent; never record against an agent that has it on.",
        )
    return check.passed(f'{origin}/ answered {{"ok": true}} with no {FAULT_FIELD} field and no {FAULT_HEADER} header')


# ── the exclusion moment ────────────────────────────────────────
def all_degraded(reputations: dict[str, Any], agent_ids: list[str]) -> bool:
    """Whether every one of `agent_ids` that has an entry reads degraded (and at least one does)."""
    entries = [reputations[a] for a in agent_ids if isinstance(reputations.get(a), dict)]
    return bool(entries) and all(e.get("degraded") is True for e in entries)


def check_below_floor(reads: Reads, facts: Facts) -> Check:
    check = Check(
        "exclusion.below_floor", "exclusion", "A registered, routable agent is genuinely below the reputation floor"
    )
    if not facts.warm:
        return check.skipped("the backend never answered", "Fix network.warm first.")
    if facts.agents is None:
        return check.skipped("GET /api/agents was not read", "Rerun once the backend answers.")
    try:
        params = reads.reputation_params()
        batch = reads.reputation()
    except Unreachable as exc:
        return check.failed(str(exc), "Rerun; the reputation routes did not answer.")
    if not params.ok or not batch.ok:
        return check.failed(
            f"reputation params {_status_line(params)}, batch {_status_line(batch)}",
            "Deploy a backend build that serves /api/stellar/reputation and /params.",
        )
    if params.obj().get("enabled") is not True:
        return check.failed(
            "reputation-gated routing is off: the floor excludes no one",
            "Set REPUTATION_ENABLED=true on the Render dashboard.",
        )
    floor = params.obj().get("floor_bps")
    if not isinstance(floor, int):
        return check.failed("the params carry no floor_bps", "Deploy a backend build whose params name the floor.")
    facts.floor_bps = floor
    registered = [str(a.get("id")) for a in facts.agents if a.get("source") == "onchain"]
    reputations = batch.obj().get("reputations") or {}
    # A cold start reads every agent degraded until the backend's first
    # ledger read answers. That is the backend waking, not a finding about
    # any agent, so the batch is read again before anything is concluded.
    reads_made = 1
    while all_degraded(reputations, registered) and reads_made < REPUTATION_READ_ATTEMPTS:
        reads.sleep(REPUTATION_REREAD_SECONDS)
        try:
            batch = reads.reputation()
        except Unreachable as exc:
            return check.failed(str(exc), "Rerun; the reputation route did not answer.")
        if not batch.ok:
            return check.failed(
                f"reputation batch {_status_line(batch)} on re-read {reads_made + 1}",
                "Rerun; the reputation route did not answer.",
            )
        reputations = batch.obj().get("reputations") or {}
        reads_made += 1
    if all_degraded(reputations, registered):
        return check.failed(
            f"every registered agent ({len(registered)}) read degraded on each of {reads_made} reads "
            f"{REPUTATION_REREAD_SECONDS:.0f} s apart: the backend could not read the ReputationLedger, so this "
            "is no verdict on any agent, not a finding that none is below the floor",
            "Check the Soroban RPC the backend reads (/api/stellar/network rpc_url) answers and that the "
            "reputation_ledger id there is right, wait a minute for the backend's ledger read, and rerun. On "
            "camera a degraded read shows '⚠ unverified' and no exclusion at all.",
        )
    facts.reputations = reputations if isinstance(reputations, dict) else {}
    reread = (
        f" (after {reads_made} reads: every agent read degraded first, the backend waking)" if reads_made > 1 else ""
    )
    rows = {str(a.get("id")): a for a in facts.agents}
    genuine: list[str] = []
    ignored: list[str] = []
    for agent_id in registered:
        rep = reputations.get(agent_id)
        if not isinstance(rep, dict) or not isinstance(rep.get("lower_bound_bps"), int):
            continue
        if rep["lower_bound_bps"] >= floor:
            continue
        if rep.get("degraded") is True or rep.get("stale") is True:
            why = "degraded" if rep.get("degraded") is True else "stale"
            ignored.append(f"{agent_id} ({rep['lower_bound_bps']} bps, {why})")
            continue
        # The planner only judges agents it could route to. An unbound one
        # renders as "no endpoint" and a delisted one not at all: neither is
        # the "excluded" row the exclusion scene films.
        if not routable(rows[agent_id]):
            why = "delisted" if rows[agent_id].get("status") == "offline" else "not bound"
            ignored.append(f"{agent_id} ({rep['lower_bound_bps']} bps, {why}: not routable, so never excluded)")
            continue
        genuine.append(agent_id)
        facts.below_floor.append(agent_id)
        facts.lower_bounds[agent_id] = rep["lower_bound_bps"]
    if genuine:
        detail = ", ".join(f"{a} ({reputations[a]['lower_bound_bps']} < {floor} bps)" for a in genuine)
        return check.passed(detail + (f"; ignored {', '.join(ignored)}" if ignored else "") + reread)
    degraded = [a for a in registered if isinstance(reputations.get(a), dict) and reputations[a].get("degraded")]
    return check.failed(
        f"no registered, bound and listed agent has an on-chain lower bound below {floor} bps"
        + (f"; ignored as not a verdict: {', '.join(ignored)}" if ignored else "")
        + (
            f"; {len(degraded)} of {len(registered)} registered agents read degraded (the ledger could not be read)"
            if degraded
            else ""
        )
        + reread,
        "Give one registered agent a real low record: paid runs rated low, or an upheld dispute, until its "
        "lower bound reads below the floor on GET /api/stellar/reputation with degraded and stale false. "
        "A degraded or stale read is the prior or an old read, never a verdict. The agent must also be bound "
        "(rebind it from its owner wallet) and listed, or the card shows 'no endpoint', not 'excluded'.",
    )


def routable(agent: dict[str, Any]) -> bool:
    """Whether the planner may route to this `GET /api/agents` row: listed and dispatchable.

    `_snapshot_registry` in app/services/orchestrator_svc.py: `_is_listed` is
    "status is not offline" (a delisting), and `is_dispatchable` is a local
    worker (every seeded agent) or a bound endpoint (an on-chain agent).
    """
    if agent.get("status") == "offline":
        return False
    if agent.get("source") == "seeded":
        return True
    return agent.get("source") == "onchain" and agent.get("bound") is True


def check_routable_count(facts: Facts) -> Check:
    check = Check(
        "exclusion.routable_count",
        "exclusion",
        f"At least {MIN_ROUTABLE_AGENTS} routable agents clear the floor, so the subject is excluded, not kept",
    )
    if facts.agents is None or facts.reputations is None or facts.floor_bps is None:
        return check.skipped("the agents or the reputation batch were not read", "Fix exclusion.below_floor first.")
    floor = facts.floor_bps
    clear: list[str] = []
    unjudged: list[str] = []
    for agent in facts.agents:
        if not routable(agent):
            continue
        agent_id = str(agent.get("id"))
        rep = facts.reputations.get(agent_id)
        if not isinstance(rep, dict) or not isinstance(rep.get("lower_bound_bps"), int):
            unjudged.append(f"{agent_id} (no reputation entry)")
            continue
        # A degraded read is the prior served because the ledger could not be
        # read, and a stale one may predate a rating: neither is a verdict, so
        # neither is counted as clearing the floor.
        if rep.get("degraded") is True or rep.get("stale") is True:
            why = "degraded" if rep.get("degraded") is True else "stale"
            unjudged.append(f"{agent_id} ({why})")
            continue
        if rep["lower_bound_bps"] >= floor:
            clear.append(agent_id)
    aside = f"; not counted: {', '.join(unjudged)}" if unjudged else ""
    if len(clear) >= MIN_ROUTABLE_AGENTS:
        return check.passed(f"{len(clear)} clear {floor} bps: {', '.join(clear)}{aside}")
    return check.failed(
        f"only {len(clear)} routable agent(s) clear {floor} bps"
        + (f" ({', '.join(clear)})" if clear else "")
        + aside
        + f": with fewer than {MIN_ROUTABLE_AGENTS}, the planner's backstop re-admits one below the floor, so the "
        "card reads 'kept below floor', not 'excluded'",
        f"Bind, or relist, agents that clear the floor until at least {MIN_ROUTABLE_AGENTS} routable ones do "
        "(listed, and seeded or bound, with a fresh read at or above the floor), then rerun. Retake S05 only "
        "after the registry is fixed.",
    )


def check_card_figure(facts: Facts) -> Check:
    check = Check(
        "exclusion.card_figure",
        "exclusion",
        "The plan card prints the excluded agent's lower bound below the floor's figure",
    )
    if not facts.below_floor or facts.floor_bps is None:
        return check.skipped("there is no genuinely below-floor agent to judge", "Fix exclusion.below_floor first.")
    floor = facts.floor_bps
    floor_figure = card_stars(floor)
    ceiling = highest_visibly_below(floor)
    blurred = [a for a in facts.below_floor if Decimal(card_stars(facts.lower_bounds[a])) >= Decimal(floor_figure)]
    if blurred:
        return check.failed(
            "; ".join(
                f"{a}'s lower bound {facts.lower_bounds[a]} bps prints as {card_stars(facts.lower_bounds[a])}, "
                f"the floor's own figure ({floor} bps prints as {floor_figure})"
                for a in blurred
            )
            + ": on camera the row reads as if the agent clears the floor",
            f"The bound must be {ceiling} bps or lower (it prints as {card_stars(ceiling)}). Give the agent one more "
            "real failed run (demo script, 'How the below-floor agent is made, honestly'), wait for a fresh read, "
            "and rerun. Never write a rating by hand.",
        )
    return check.passed(
        ", ".join(f"{a}: {card_stars(facts.lower_bounds[a])} ({facts.lower_bounds[a]} bps)" for a in facts.below_floor)
        + f" against the floor's {floor_figure} ({floor} bps); at most {ceiling} bps prints below it"
    )


def check_decompose(reads: Reads, facts: Facts, intent: str | None) -> Check:
    check = Check(
        "exclusion.decompose",
        "exclusion",
        "A real plan excludes or substitutes the below-floor agent",
        required=intent is not None,
    )
    if intent is None:
        return check.skipped(
            "not asked for (it costs a model call and stores a plan)",
            'Pass --with-decompose "<the intent the video uses>" to confirm the plan names the exclusion.',
        )
    if not facts.below_floor:
        return check.skipped("there is no genuinely below-floor agent to look for", "Fix exclusion.below_floor first.")
    try:
        answer = reads.decompose(intent)
    except Unreachable as exc:
        return check.failed(str(exc), "Rerun; the decompose did not answer (it is not retried: it is a write).")
    if not answer.ok:
        return check.failed(f"decompose answered {_status_line(answer)}", "Read the backend log for the decompose.")
    notices = [n for n in answer.obj().get("notices") or [] if isinstance(n, dict)]
    hits = [
        n for n in notices if n.get("kind") in ("excluded", "substituted") and n.get("agent_id") in facts.below_floor
    ]
    plan_id = answer.obj().get("plan_id")
    if hits:
        return check.passed(
            f"plan {plan_id}: " + "; ".join(f"{n.get('kind')} {n.get('agent_id')} ({n.get('reason')})" for n in hits)
        )
    return check.failed(
        f"plan {plan_id} has {len(notices)} notice(s), none excluding or substituting {', '.join(facts.below_floor)}",
        "Use an intent that routes to that agent's skill, so the floor has it to exclude.",
    )


# ── wallets ─────────────────────────────────────────────────────
def check_buyer(chain: ChainReader, cfg: RunConfig) -> Check:
    check = Check("wallets.buyer", "wallets", "The buyer wallet exists and can pay the plan cap plus fees")
    if cfg.buyer is None:
        return check.skipped("no --buyer given", "Pass --buyer G… (the wallet the recording pays from).")
    need = cfg.cap + BUYER_FEE_ALLOWANCE
    return _funded(check, chain, cfg.buyer, need, f"--cap {cfg.cap} + {BUYER_FEE_ALLOWANCE} fees")


def check_operator(chain: ChainReader, facts: Facts, cfg: RunConfig) -> Check:
    check = Check("wallets.operator", "wallets", "The operator wallet exists, is funded, and owns a bound agent")
    if cfg.operator is None:
        return check.skipped("no --operator given", "Pass --operator G… (the wallet shown registering and binding).")
    funded = _funded(check, chain, cfg.operator, OPERATOR_FEE_ALLOWANCE, f"{OPERATOR_FEE_ALLOWANCE} XLM of fees")
    if funded.status != PASS:
        return funded
    if facts.agents is None:
        return check.skipped("GET /api/agents was not read", "Rerun once the backend answers.")
    owned = [
        str(a.get("id"))
        for a in facts.agents
        if a.get("owner") == cfg.operator and a.get("source") == "onchain" and a.get("bound") is True
    ]
    if not owned:
        return check.failed(
            f"{cfg.operator} owns no registered agent with a bound endpoint in GET /api/agents",
            "Have the operator register an agent from this wallet (/app/register) and bind it (/app/bind).",
        )
    return check.passed(funded.detail + f"; owns {', '.join(owned)}")


def check_team_wallets(team: TeamRegister, facts: Facts, cfg: RunConfig) -> Check:
    check = Check("wallets.team", "wallets", "Neither the buyer nor the operator is a team wallet")
    named = [(label, account) for label, account in (("buyer", cfg.buyer), ("operator", cfg.operator)) if account]
    if not named:
        return check.skipped("no --buyer or --operator given", "Pass --buyer and --operator.")
    hits = [(label, account, team.role(account)) for label, account in named if team.role(account)]
    if not hits:
        return check.passed(f"none of {', '.join(a for _, a in named)} is in {team.path}")
    listed = "; ".join(f"the {label} {account} is the team's {role!r}" for label, account, role in hits)
    if not cfg.allow_team_operator:
        return check.failed(
            f"{listed} ({team.path})",
            "Use wallets the team does not control, or pass --allow-team-operator and disclose it on camera.",
        )
    for label, account, role in hits:
        facts.disclosures.append(f"The {label} {account} is a team wallet ({role}): say so on camera.")
    return check.warned(listed, "Disclose on camera, and in the video description, that this wallet is the team's.")


# ── the frontend ────────────────────────────────────────────────
def check_page(reads: Reads, path: str) -> Check:
    check = Check(f"frontend.{path}", "frontend", f"{path} answers 200 without a login")
    try:
        answer = reads.page(path)
    except Unreachable as exc:
        return check.failed(str(exc), "Check the Vercel deployment behind --frontend is up.")
    if answer.ok:
        return check.passed(f"{answer.url} 200")
    where = f" -> {answer.location}" if answer.location else ""
    return check.failed(
        f"{answer.url} answered {answer.status}{where}",
        "Deploy the frontend build that has this page, and make sure it needs no login.",
    )


# ── the run ─────────────────────────────────────────────────────
def run_checks(
    cfg: RunConfig,
    reads: Reads,
    chain: ChainReader,
    team: TeamRegister,
    *,
    warmup_budget: float = WARMUP_BUDGET_SECONDS,
    progress: Callable[[Check], None] | None = None,
) -> tuple[list[Check], Facts]:
    """Every check, in order. Raises `Refused` before judging anything on a non-testnet answer."""
    refuse_unless_testnet(chain)
    facts = Facts()
    checks: list[Check] = []

    def add(check: Check) -> None:
        checks.append(check)
        if progress is not None:
            progress(check)

    add(check_warm(reads, facts, warmup_budget))
    add(check_api_network(reads, facts))
    if facts.warm:
        try:
            agents = reads.agents()
            if agents.ok and isinstance(agents.body, list):
                facts.agents = [a for a in agents.body if isinstance(a, dict)]
        except Unreachable:
            facts.agents = None
    add(check_adoption_route(reads, facts))
    add(check_agent_readiness_route(reads, facts))
    add(read_backend_readiness(reads, facts))
    add(check_escrow_version(chain, facts))
    add(check_escrow_settler(chain, facts))
    add(check_refunds_enabled(facts))
    add(check_dispute_store(facts))
    add(check_settler_balance(chain, facts, cfg))
    # The floor is read before the operator group: the agent it excludes is
    # meant to fail `routable`, and operator.ready has to know which it is.
    add(check_below_floor(reads, facts))
    add(check_card_figure(facts))
    add(check_routable_count(facts))
    add(check_decompose(reads, facts, cfg.decompose_intent))
    add(check_external_count(facts))
    add(check_external_ready(reads, facts))
    add(check_operator_endpoint(reads, cfg))
    add(check_buyer(chain, cfg))
    add(check_operator(chain, facts, cfg))
    add(check_team_wallets(team, facts, cfg))
    for path in FRONTEND_PAGES:
        add(check_page(reads, path))
    return checks, facts
