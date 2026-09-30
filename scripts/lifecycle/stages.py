"""The nine stages, and the runner that sequences, records and resumes them.

    decompose → authorize → execute → poll → verify → dispute → uphold → refund → reputation

Each stage reads what it needs from `RunState`, does its one job against the
deployed API, writes its evidence rows the moment it has them, saves state, and
returns. A stage that cannot go on raises `Stop` with an exit code; the runner
records where it stopped — in the evidence file and in state — and exits.

Three rules shape every stage:

  * **The ledger is asked, never assumed.** Every row with a hash carries the
    status `ChainReader.observe` read back for it.
  * **A write with an unknown outcome is never repeated.** A submit, an
    execute or an uphold whose answer is lost is followed by a READ of the
    state it would have changed, a row saying what the read found, and a stop.
    A human decides what happens next; `--from-task` / `--from-dispute`
    carry the run on once they have.
  * **The dApp's API, the dApp's way.** Bodies, headers and the order of
    calls are the frontend's (see `api.py` for each call's source).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from stellar_sdk import Keypair

from . import verify
from .api import ApiError, OrizonApi, UnknownOutcome
from .chain import ChainEvent, ChainReader, Observation, RpcError, SimulationError
from .config import (
    EXIT_OK,
    EXIT_PLAN_MISSING_AGENT,
    EXIT_REFUSED,
    EXIT_RESUME_CONFLICT,
    EXIT_STAGE_FAILED,
    EXIT_TIMED_OUT,
    EXIT_UNKNOWN_OUTCOME,
    EXIT_VERIFY_FAILED,
    MIN_AUTHORIZE_AMOUNT,
    STAGES,
    TESTNET_PASSPHRASE,
    Budgets,
    RunConfig,
    authorize_label,
    authorize_ttl,
    stage_index,
)
from .evidence import EvidenceLog, EvidenceRow, RunState, StateStore, tx_row, utc_now
from .redact import Console
from .retry import RetryPolicy
from .signing import (
    AuthorizeCall,
    SigningRefused,
    auth_id_from_return_value,
    check_dispute_message,
    check_read_message,
    load_api_key,
    load_keypair,
    sign_authorize,
    sign_message_b64,
    usdc_to_stroops,
)

# The trace line a landed rating writes, and the one an unlanded rating
# writes — app/services/execution_svc.py `_submit_ratings`:
#   f"reputation → {step.agent_name} rated {rating}/100 · tx {tx[:10]}…"
#   f"reputation submit failed for {step.agent_name}: {reason} · tx {tx[:10]}…"
# The trace carries only the first ten hex characters of the hash; the full
# hash is recovered from the ReputationLedger's `rated` event (`rating_hashes`).
RATED_LINE = re.compile(r"^reputation → (?P<name>.+) rated (?P<rating>\d+)/100 · tx (?P<prefix>[0-9a-f]{10})…$")
UNLANDED_LINE = re.compile(r"^reputation submit failed for (?P<name>.+?): .* · tx (?P<prefix>[0-9a-f]{10})…$")

TERMINAL_TASK = ("complete", "failed")
RESUME_SCAN_LEDGERS = 17_280


class Stop(Exception):
    """The run cannot go on. `code` is the exit code; `message` says why and what next."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def asset_label(asset: str | None) -> str:
    """What amounts are denominated in. `total_usdc` is a legacy name: on
    testnet the escrow's SAC wraps native XLM (lib/money.ts assetLabel)."""
    return "XLM (native)" if asset in (None, "native") else str(asset)


def fmt_amount(value: float | int | None) -> str | None:
    return None if value is None else f"{float(value):.7f}"


@dataclass
class Runner:
    cfg: RunConfig
    budgets: Budgets
    api: OrizonApi
    http: httpx.Client
    console: Console
    log: EvidenceLog
    store: StateStore
    environ: dict[str, str]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    state: RunState = field(default_factory=RunState)
    network: dict[str, Any] = field(default_factory=dict)
    chain: ChainReader | None = None
    buyer: Keypair | None = None
    adjudicator_key: str | None = None
    # The dispute read grant is a credential for its hour: memory only, never
    # saved, never printed.
    grant: str | None = None
    rating_seen: int = 0
    # agent name -> id from GET /api/agents, for a resumed run with no plan.
    directory: dict[str, str] = field(default_factory=dict)
    # What a dry run found that would stop the real run it rehearses. A dry
    # run with any of these exits EXIT_REFUSED, naming each, never 0.
    blockers: list[str] = field(default_factory=list)

    # ── output ──────────────────────────────────────────────────
    def say(self, line: str = "") -> None:
        self.console.say(line)

    def row(self, row: EvidenceRow) -> EvidenceRow:
        """Append one evidence row — unless this is a dry run, which writes nothing."""
        if self.cfg.dry_run:
            return row
        self.log.append(row)
        return row

    def note(self, stage: str, event: str, summary: str, **detail: Any) -> EvidenceRow:
        return self.row(
            EvidenceRow(
                stage=stage,
                event=event,
                utc=utc_now(),
                network=self.state.network or "unknown",
                run_id=self.state.run_id,
                agent=self.cfg.agent,
                buyer=self.state.buyer or None,
                detail={"summary": summary, **detail},
            )
        )

    def tx(
        self,
        stage: str,
        event: str,
        seen: Observation,
        *,
        contract: str | None,
        agent: str | None = None,
        amount: float | int | None = None,
        summary: str = "",
        **detail: Any,
    ) -> EvidenceRow:
        row = self.row(
            tx_row(
                stage=stage,
                event=event,
                network=self.state.network,
                run_id=self.state.run_id,
                tx_hash=seen.tx_hash,
                onchain_status=seen.status,
                contract=contract,
                agent=agent or self.cfg.agent,
                buyer=self.state.buyer,
                amount=fmt_amount(amount),
                asset=asset_label(self.network.get("asset")) if amount is not None else None,
                detail={"summary": summary, "ledger": seen.ledger, "read_from": seen.source, **detail},
            )
        )
        self.say(f"  [{stage}] {event}: {seen.tx_hash} -> {seen.status} (ledger {seen.ledger}) {row.explorer}")
        return row

    def save(self) -> None:
        if not self.cfg.dry_run:
            self.store.save(self.state)

    @property
    def contracts(self) -> dict[str, str]:
        return dict(self.network.get("contracts") or {})

    @property
    def agents_label(self) -> str:
        """Every --agent, for a sentence: exactly `cfg.agent` for a single-agent run."""
        return ", ".join(self.cfg.all_agents)

    def need_chain(self) -> ChainReader:
        if self.chain is None:
            raise Stop(EXIT_REFUSED, "no RPC reader: the network was never read")
        return self.chain

    def need_buyer(self) -> Keypair:
        if self.buyer is None:
            raise Stop(EXIT_REFUSED, "no buyer key loaded")
        return self.buyer

    # ── the run ─────────────────────────────────────────────────
    def run(self) -> int:
        try:
            start = self.preflight()
            if self.cfg.dry_run:
                self.print_plan(start)
                if self.blockers:
                    self.say("")
                    self.say("A real run with these flags would stop:")
                    for blocker in self.blockers:
                        self.say(f"  - {blocker}")
                    raise Stop(
                        EXIT_REFUSED,
                        f"dry run: {len(self.blockers)} blocker(s) a real run would hit: " + "; ".join(self.blockers),
                    )
                return EXIT_OK
            last = stage_index(self.cfg.until)
            for name in STAGES[stage_index(start) : last + 1]:
                self.say(f"== {name}")
                getattr(self, f"stage_{name}")()
                self.state.done(name)
                self.save()
            self.state.stopped = None
            self.save()
            self.say(f"done through '{self.cfg.until}'. Evidence: {self.log.jsonl} and {self.log.markdown}")
            return EXIT_OK
        except SigningRefused as exc:
            return self.stopped(Stop(EXIT_STAGE_FAILED if self.state.run_id else EXIT_REFUSED, str(exc)))
        except Stop as exc:
            return self.stopped(exc)
        except KeyboardInterrupt:
            if self.state.run_id and not self.cfg.dry_run:
                self.note("run", "run_interrupted", "interrupted by the operator; state saved")
            raise
        except Exception as exc:
            # Evidence already on disk stays; say where it stopped, then crash
            # loudly. The exception text goes through the redactor like
            # everything else.
            if self.state.run_id and not self.cfg.dry_run:
                self.note("run", "run_crashed", f"{type(exc).__name__}: {self.console.redactor.scrub(str(exc))}")
            raise

    def stopped(self, exc: Stop) -> int:
        self.say(f"STOPPED (exit {exc.code}): {exc.message}")
        if self.state.run_id and not self.cfg.dry_run and self.log.directory.exists():
            message = self.console.redactor.scrub(exc.message)
            self.state.stopped = {"code": exc.code, "message": message, "utc": utc_now()}
            self.save()
            self.note("run", "run_stopped", exc.message, exit_code=exc.code)
        return exc.code

    # ── preflight (every invocation, dry runs included) ─────────
    def preflight(self) -> str:
        self.warm()
        self.network = self.api.network()
        passphrase = self.network.get("network_passphrase")
        if passphrase != TESTNET_PASSPHRASE:
            raise Stop(
                EXIT_REFUSED,
                f"refusing to run: the API is on {self.network.get('network')!r} ({passphrase!r}); "
                "this harness signs on testnet only",
            )
        rpc_url = self.cfg.rpc_url or str(self.network.get("rpc_url") or "")
        self.chain = ChainReader(
            client=self.http,
            rpc_url=rpc_url,
            horizon_url=self.cfg.horizon_url,
            passphrase=TESTNET_PASSPHRASE,
            retry=self.retry,
            sleep=self.sleep,
            clock=self.clock,
        )
        rpc_passphrase = self.chain.network_passphrase()
        if rpc_passphrase != TESTNET_PASSPHRASE:
            raise Stop(EXIT_REFUSED, f"refusing to run: the RPC at {rpc_url} is on {rpc_passphrase!r}, not testnet")

        self.buyer = load_keypair(self.cfg.buyer_secret_env, self.console.redactor, self.environ)
        needs_uphold = stage_index(self.cfg.until) >= stage_index("uphold")
        if needs_uphold:
            if self.cfg.adjudicator_key_env:
                self.adjudicator_key = load_api_key(self.cfg.adjudicator_key_env, self.console.redactor, self.environ)
            elif not self.cfg.dry_run:
                raise Stop(EXIT_REFUSED, "--adjudicator-key-env is required to run through 'uphold'")
            else:
                # A dry run signs and adjudicates nothing, so it does not ask
                # for the operator key; the real run still will.
                self.say("NOTE: the real run through 'uphold' needs --adjudicator-key-env; this dry run does not")

        start = self.resume_point()
        version, how = self.chain.escrow_version(self.contracts["payment_escrow"], self.need_buyer().public_key)
        run_escrow = (self.state.authorize or {}).get("escrow")
        if run_escrow and run_escrow != self.contracts["payment_escrow"]:
            # A resume across the escrow switch: this run's money went through
            # the escrow it authorized against, so that is what gets verified.
            self.say(
                f"NOTE: this run authorized against {run_escrow}; the API now names {self.contracts['payment_escrow']}"
            )
            version, how = self.chain.escrow_version(run_escrow, self.need_buyer().public_key)
        self.state.escrow_version = version
        self.state.network = str(self.network.get("network") or "testnet")
        self.state.agent = self.cfg.agent
        self.state.buyer = self.need_buyer().public_key

        listed = self.api.agents()
        agents = {a.get("id"): a for a in listed}
        self.directory = {str(a.get("name")): str(a.get("id")) for a in listed if a.get("name")}
        readiness = self.api.readiness()
        self.say(f"network: {self.state.network} · escrow v{version} ({how}) · buyer {self.state.buyer}")
        self.say(f"api {self.api.base} · rpc {rpc_url}")
        if readiness is not None:
            self.say(f"readiness: {readiness.get('status')} · ratings {readiness.get('ratings')}")
        records: dict[str, dict[str, Any]] = {}
        for agent_id in self.cfg.all_agents:
            agent = agents.get(agent_id)
            if agent is None:
                raise Stop(EXIT_REFUSED, f"agent {agent_id!r} is not listed by GET /api/agents")
            records[agent_id] = agent
            self.say(
                f"agent {agent_id}: source={agent.get('source')} bound={agent.get('bound')} "
                f"owner={agent.get('owner')} price={agent.get('price')}"
            )
            external = agent.get("source") == "onchain" and agent.get("bound") is True
            if not external and start == "decompose":
                message = (
                    f"agent {agent_id!r} is not an external, bound agent "
                    f"(source={agent.get('source')}, bound={agent.get('bound')})"
                )
                if not self.cfg.dry_run:
                    raise Stop(EXIT_REFUSED, message)
                self.blockers.append(f"{message}: a real run refuses it before decompose (exit {EXIT_REFUSED})")

        if not self.cfg.dry_run:
            self.save()
            self.note(
                "preflight",
                "preflight",
                f"testnet confirmed by API and RPC; escrow v{version}; starting at '{start}'",
                escrow_version=version,
                escrow_version_source=how,
                escrow=self.contracts.get("payment_escrow"),
                readiness=readiness,
                agent_record=records[self.cfg.agent],
                start_stage=start,
                until=self.cfg.until,
                **({"agents": list(self.cfg.all_agents), "agent_records": records} if self.cfg.multi_agent else {}),
            )
        self.snapshot("start" if start == "decompose" else f"resume_at_{start}")
        return start

    def warm(self) -> None:
        """Wake the backend the way components/backend-warmup.tsx does — GET
        /api/health — but wait for the answer: Render's free tier takes 30-60 s
        to boot, and the first real request should not be the one that eats it."""
        deadline = self.clock() + self.budgets.warmup
        delay = 2.0
        while True:
            try:
                self.api.health()
                return
            except (httpx.TransportError, ApiError) as exc:
                if self.clock() + delay > deadline:
                    raise Stop(
                        EXIT_REFUSED, f"the backend did not wake within {self.budgets.warmup:.0f}s: {exc}"
                    ) from exc
                self.say(f"  waiting for the backend to wake ({type(exc).__name__})")
                self.sleep(delay)
                delay = min(delay * 2, 15.0)

    def resume_point(self) -> str:
        """Where this invocation starts, and whether the evidence dir allows it."""
        prior = self.store.load()
        if prior is not None:
            if prior.agent and prior.agent != self.cfg.agent:
                raise Stop(
                    EXIT_RESUME_CONFLICT, f"{self.store.path} is agent {prior.agent}'s run, not {self.cfg.agent}'s"
                )
            if prior.buyer and self.buyer is not None and prior.buyer != self.buyer.public_key:
                raise Stop(EXIT_RESUME_CONFLICT, f"{self.store.path} is buyer {prior.buyer}'s run, not this key's")
            self.state = prior
        if self.cfg.from_dispute:
            if self.state.dispute is None or self.state.dispute.get("id") != self.cfg.from_dispute:
                self.state.dispute = {"id": self.cfg.from_dispute}
            self.ensure_run_id()
            return "uphold"
        if self.cfg.from_task:
            if self.state.task_id != self.cfg.from_task:
                # Another task's context would verify the wrong money: drop it.
                self.state.task_id = self.cfg.from_task
                self.state.read_token = None
                self.state.plan = self.state.authorize = self.state.settlement = self.state.dispute = None
                self.state.balances_before = self.state.owners = None
                self.state.start_ledger = None
            self.ensure_run_id()
            return "poll"
        if prior is not None and (prior.plan or prior.authorize or prior.task_id):
            pending = (prior.authorize or {}).get("status") in ("signed", "unknown")
            raise Stop(
                EXIT_RESUME_CONFLICT,
                f"{self.store.path} already holds a run (task {prior.task_id}"
                + (f", authorize {prior.authorize['tx_hash']} unconfirmed" if pending and prior.authorize else "")
                + "). Resume it with --from-task / --from-dispute, or start a new run in a new --evidence-dir. "
                "A fresh run here could authorize a second payment.",
            )
        if not self.cfg.intent:
            raise Stop(EXIT_REFUSED, "--intent is required for a fresh run")
        self.ensure_run_id()
        return "decompose"

    def ensure_run_id(self) -> None:
        if not self.state.run_id:
            self.state.run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{self.cfg.agent}"

    def print_plan(self, start: str) -> None:
        buyer = self.need_buyer().public_key
        chain = self.need_chain()
        stages = STAGES[stage_index(start) : stage_index(self.cfg.until) + 1]
        sac = self.network.get("asset_sac")
        if sac:
            balance = chain.sac_balance(str(sac), buyer, buyer)
            if balance is None:
                self.say(f"buyer {buyer} does not exist on testnet: fund it (friendbot) before a real run")
                if "decompose" in stages:
                    self.blockers.append(
                        f"the buyer {buyer} does not exist on testnet: a real run refuses it after decompose, "
                        f"before anything is signed (exit {EXIT_REFUSED}); fund it (friendbot) first"
                    )
            else:
                self.say(f"buyer balance: {balance} stroops of {asset_label(self.network.get('asset'))}")
        version = self.state.escrow_version or 1
        if version < 2 and "verify" in stages:
            escrow = (self.state.authorize or {}).get("escrow") or self.contracts.get("payment_escrow")
            self.blockers.append(
                f"the escrow {escrow} is v1, which cannot settle an external payer's funds (D-039): a real run "
                f"stops at 'verify' with exit {EXIT_VERIFY_FAILED}, after the buyer has signed; deploy escrow v2 first"
            )
        self.say("")
        self.say("DRY RUN — nothing was built, signed or submitted. The plan:")
        steps = {
            "decompose": f"POST /api/orchestrator/decompose {{intent}}; "
            f"refuse unless the plan routes to {self.agents_label}",
            "authorize": (
                f"POST /api/stellar/build/authorize {{payer: {buyer}, "
                f"agent_id: {authorize_label(self.state.escrow_version or 1, '<plan_id>')}, "
                f"max_amount_usdc: plan.total_usdc or {MIN_AUTHORIZE_AMOUNT}, "
                f"ttl_seconds: {authorize_ttl(self.state.escrow_version or 1)}}}; "
                f"check it is PaymentEscrow({self.contracts.get('payment_escrow')}).authorize; sign with "
                f"${self.cfg.buyer_secret_env}; POST /api/stellar/submit {{signed_xdr}} ONCE"
            ),
            "execute": "POST /api/orchestrator/execute {plan_id, auth_id_hex, payer} ONCE",
            "poll": "GET /api/tasks/{id} + GET /api/trace/{id} (X-Task-Token) to terminal; "
            "snapshot reputation per rating",
            "verify": (
                "GET /api/tasks/{id}/disputes; getTransaction + getEvents on the escrow "
                f"(v{self.state.escrow_version} checks); "
                "AttestationRegistry.get(job_id)"
            ),
            "dispute": "POST /api/disputes/challenge; SEP-53 sign; POST /api/disputes; "
            "read grant via /api/disputes/read-*",
            "uphold": "POST /api/disputes/{id}/uphold with X-API-Key from "
            f"${self.cfg.adjudicator_key_env or '<the --adjudicator-key-env variable>'} ONCE",
            "refund": "GET /api/disputes/{id} until credited with a confirmed rating; getTransaction on both",
            "reputation": "GET /api/stellar/reputation; compare start / after ratings / after dispute",
        }
        for name in stages:
            self.say(f"  {name:<10} {steps[name]}")
        self.say(f"evidence would be appended to {self.log.jsonl}")

    # ── reputation snapshots ────────────────────────────────────
    def snapshot(self, label: str) -> dict[str, Any] | None:
        """The agent's standing as the agents page reads it (GET /api/stellar/reputation)."""
        batch = self.api.reputation_batch()
        info = (batch.get("reputations") or {}).get(self.cfg.agent)
        if info is None:
            self.note("reputation", "reputation_snapshot", f"{label}: agent not in the reputation batch", label=label)
            self.say(f"  reputation[{label}]: not in the batch")
            return None
        fields = (
            "smoothed_bps",
            "lower_bound_bps",
            "avg_bps",
            "source",
            "count",
            "disputed",
            "dispute_rate_bps",
            "stale",
            "degraded",
        )
        detail = {k: info.get(k) for k in fields}
        self.note(
            "reputation",
            "reputation_snapshot",
            f"{label}: score {detail['smoothed_bps']} lower {detail['lower_bound_bps']} "
            f"source {detail['source']} count {detail['count']}",
            label=label,
            floor_bps=batch.get("floor_bps"),
            **detail,
        )
        self.say(
            f"  reputation[{label}]: score {detail['smoothed_bps']} bps, lower bound {detail['lower_bound_bps']}, "
            f"source {detail['source']}, count {detail['count']}, disputed {detail['disputed']}"
        )
        return detail

    # ── 1. decompose ────────────────────────────────────────────
    def stage_decompose(self) -> None:
        assert self.cfg.intent is not None
        plan = self.api.decompose(self.cfg.intent)
        steps = [
            {
                "agent_id": s.get("agent_id"),
                "agent_name": s.get("agent_name"),
                "est_price_usdc": s.get("est_price_usdc"),
            }
            for s in plan.get("steps") or []
        ]
        routed = [s["agent_id"] for s in steps]
        self.say(f"  plan {plan.get('plan_id')}: {routed} total {plan.get('total_usdc')}")
        missing = [a for a in self.cfg.all_agents if a not in routed]
        if missing:
            self.note(
                "decompose",
                "plan_missing_agent",
                f"plan routes to {routed}, not {', '.join(missing)}",
                plan_id=plan.get("plan_id"),
                **({"missing": missing} if self.cfg.multi_agent else {}),
            )
            raise Stop(
                EXIT_PLAN_MISSING_AGENT,
                f"the plan routes to {routed}, not {', '.join(missing)}; nothing was signed. "
                + (
                    "Reword --intent so the planner needs every agent's skills."
                    if self.cfg.multi_agent
                    else "Reword --intent toward the agent's skills."
                ),
            )
        self.state.plan = {
            "plan_id": plan.get("plan_id"),
            "total_usdc": plan.get("total_usdc"),
            "steps": steps,
            "planner_fallback": plan.get("planner_fallback"),
            "reputation_degraded": plan.get("reputation_degraded"),
        }
        self.save()
        self.note(
            "decompose",
            "plan",
            f"plan {plan.get('plan_id')} routes {routed}, total {plan.get('total_usdc')}",
            plan=self.state.plan,
            notices=plan.get("notices"),
            floor_bps=plan.get("floor_bps"),
        )
        self.balances_before(routed)

    def balances_before(self, agent_ids: list[str]) -> None:
        """Owners and SAC balances before any money moves, for the v2 deltas."""
        chain, buyer = self.need_chain(), self.need_buyer().public_key
        owners = self.owners_for(agent_ids)
        sac = str(self.network.get("asset_sac") or "")
        read = {addr: chain.sac_balance(sac, addr, buyer) for addr in dict.fromkeys([buyer, *owners.values()])}
        if read.get(buyer) is None:
            raise Stop(EXIT_REFUSED, f"the buyer {buyer} does not exist on testnet; fund it (friendbot) first")
        balances = {addr: value for addr, value in read.items() if value is not None}
        self.state.owners = owners
        self.state.balances_before = balances
        self.save()
        self.note(
            "decompose",
            "balances_before",
            f"{len(balances)} balance(s) read before authorize",
            balances=balances,
            owners=owners,
        )

    def owners_for(self, agent_ids: list[str]) -> dict[str, str]:
        """agent id -> owner, from the LIVE registry (GET /api/stellar/agent/{id}).
        A seeded agent has no registry record and no owner, and is left out."""
        owners: dict[str, str] = {}
        for agent_id in dict.fromkeys(agent_ids):
            try:
                owner = (self.api.registry_agent(agent_id).get("agent") or {}).get("owner")
            except ApiError:
                owner = None
            if owner:
                owners[agent_id] = str(owner)
        return owners

    # ── 2. authorize ────────────────────────────────────────────
    def stage_authorize(self) -> None:
        chain, kp = self.need_chain(), self.need_buyer()
        plan = self.state.plan or {}
        # `plan.total_usdc > 0 ? plan.total_usdc : MIN_CAP`
        max_amount = float(plan.get("total_usdc") or 0) or MIN_AUTHORIZE_AMOUNT
        version = self.state.escrow_version or 1
        label = authorize_label(version, str(plan.get("plan_id")))
        ttl = authorize_ttl(version)
        built = self.api.build_authorize(kp.public_key, max_amount, ttl, label)
        expect = AuthorizeCall(
            escrow=self.contracts["payment_escrow"],
            payer=kp.public_key,
            agent_id=label,
            max_stroops=usdc_to_stroops(max_amount),
        )
        signed = sign_authorize(str(built["xdr"]), kp, TESTNET_PASSPHRASE, expect)
        self.console.redactor.register(signed.signed_xdr)
        if self.state.start_ledger is None:
            self.state.start_ledger = chain.latest_ledger()
        # On disk BEFORE the send: a crash between here and the answer leaves
        # the hash to look up, and blocks a fresh run from paying twice.
        self.state.authorize = {
            "tx_hash": signed.tx_hash,
            "status": "signed",
            "max_amount_usdc": max_amount,
            "max_stroops": expect.max_stroops,
            "expires_at": signed.expires_at,
            "label": label,
            "ttl_seconds": ttl,
            "escrow": expect.escrow,
        }
        self.save()
        self.say(f"  signed authorize {signed.tx_hash} for {max_amount} (expires {signed.expires_at})")

        try:
            result = self.api.submit(signed.signed_xdr)
        except UnknownOutcome as exc:
            self.unknown_authorize(signed.tx_hash, max_amount, str(exc))
        except ApiError as exc:
            self.state.authorize["status"] = "refused"
            self.save()
            self.note("authorize", "authorize_refused", f"{exc.status} {exc.code}", tx_hash_not_sent=signed.tx_hash)
            raise Stop(EXIT_STAGE_FAILED, f"submit refused before sending: {exc}") from exc
        if result.get("status") == "timeout":
            self.unknown_authorize(signed.tx_hash, max_amount, "the backend's poll timed out")

        tx_hash = str(result.get("hash") or signed.tx_hash)
        seen = chain.observe(tx_hash, self.budgets.tx_observe)
        self.tx(
            "authorize",
            "authorize",
            seen,
            contract=self.contracts["payment_escrow"],
            amount=max_amount,
            summary=f"PaymentEscrow.authorize labelled {label}; API said {result.get('status')}",
            api_status=result.get("status"),
            expires_at=signed.expires_at,
        )
        if seen.status != "SUCCESS":
            self.state.authorize["status"] = seen.status
            self.save()
            code = EXIT_VERIFY_FAILED if result.get("status") == "SUCCESS" else EXIT_STAGE_FAILED
            raise Stop(code, f"authorize {tx_hash} is {seen.status} on the ledger (API said {result.get('status')})")
        auth_id = auth_id_from_return_value(result.get("return_value"))
        if auth_id is None:
            raise Stop(EXIT_STAGE_FAILED, "failed to read auth_id from the authorize result")
        self.state.authorize.update({"status": "SUCCESS", "auth_id_hex": auth_id, "ledger": seen.ledger})
        self.save()

    def unknown_authorize(self, tx_hash: str, max_amount: float, reason: str) -> None:
        seen = self.need_chain().observe(tx_hash, self.budgets.tx_observe)
        assert self.state.authorize is not None
        self.state.authorize["status"] = "unknown"
        self.save()
        self.tx(
            "authorize",
            "authorize_unknown",
            seen,
            contract=self.contracts["payment_escrow"],
            amount=max_amount,
            summary=f"submit outcome unknown ({reason}); ledger read back",
        )
        raise Stop(
            EXIT_UNKNOWN_OUTCOME,
            f"the authorize submit's outcome is unknown ({reason}); the ledger says {seen.status} for {tx_hash}. "
            "Not resubmitted. Look it up before anything else.",
        )

    # ── 3. execute ──────────────────────────────────────────────
    def stage_execute(self) -> None:
        plan, auth = self.state.plan or {}, self.state.authorize or {}
        buyer = self.need_buyer().public_key
        try:
            started = self.api.execute(str(plan["plan_id"]), str(auth["auth_id_hex"]), buyer)
        except UnknownOutcome as exc:
            self.note("execute", "execute_unknown", str(exc), plan_id=plan.get("plan_id"))
            raise Stop(
                EXIT_UNKNOWN_OUTCOME,
                f"execute's outcome is unknown ({exc.reason}). Not retried: a second execute would start a second "
                "workflow on the same authorization. Find the task in the console before resuming with --from-task.",
            ) from exc
        except ApiError as exc:
            self.note("execute", "execute_refused", f"{exc.status} {exc.code}")
            raise Stop(EXIT_STAGE_FAILED, f"execute refused: {exc}") from exc
        self.state.task_id = str(started["task_id"])
        self.state.read_token = started.get("read_token")
        self.console.redactor.register(self.state.read_token)
        self.save()
        self.note("execute", "execute", f"task {self.state.task_id} started", task_id=self.state.task_id)
        self.say(f"  task {self.state.task_id}")

    # ── 4. poll ─────────────────────────────────────────────────
    def stage_poll(self) -> None:
        task_id = self.state.task_id
        assert task_id is not None
        self.console.redactor.register(self.state.read_token)
        deadline = self.clock() + self.budgets.task
        lines: list[dict[str, Any]] = []
        names = self.agent_names()
        while True:
            try:
                task = self.api.task(task_id, self.state.read_token)
            except ApiError as exc:
                if exc.status != 404:
                    raise
                # The task lives in the backend's memory, so a restart (the AC4
                # recipe) forgets it; the settlement it wrote survives in the
                # store, and `verify` reads it from there.
                self.note(
                    "poll", "task_not_in_memory", "GET /api/tasks/{id} is 404: the backend restarted or evicted it"
                )
                self.say("  task not in the backend's memory (restart?); verifying from the settlement record")
                return
            trace = self.api.trace(task_id, self.state.read_token)
            for line in trace[len(lines) :]:
                lines.append(line)
                self.on_trace_line(line, names)
            if task.get("status") in TERMINAL_TASK:
                break
            if self.clock() + self.budgets.poll_interval > deadline:
                self.note("poll", "task_timeout", f"task still {task.get('status')} after {self.budgets.task:.0f}s")
                raise Stop(EXIT_TIMED_OUT, f"task {task_id} still {task.get('status')} after {self.budgets.task:.0f}s")
            self.sleep(self.budgets.poll_interval)

        self.note(
            "poll",
            "task_terminal",
            f"task {task.get('status')}, spent {task.get('spent')}",
            status=task.get("status"),
            spent=task.get("spent"),
            charge_tx=task.get("charge_tx"),
            proof_tx=task.get("proof_tx"),
            trace_lines=len(lines),
        )
        self.record_ratings(lines, names)
        self.snapshot("after_ratings")

    def agent_names(self) -> dict[str, str]:
        """agent_name -> agent_id for the plan's steps (the trace names agents by name)."""
        names = dict(self.directory)
        steps = (self.state.plan or {}).get("steps") or []
        names.update({str(s.get("agent_name")): str(s.get("agent_id")) for s in steps if s.get("agent_name")})
        return names

    def on_trace_line(self, line: dict[str, Any], names: dict[str, str]) -> None:
        """Snapshot the agent's standing each time the trace says it was rated."""
        rated = RATED_LINE.match(str(line.get("msg") or ""))
        if rated and names.get(rated.group("name"), rated.group("name")) == self.cfg.agent:
            self.rating_seen += 1
            self.snapshot(f"after_rating_{self.rating_seen}")

    def record_ratings(self, lines: list[dict[str, Any]], names: dict[str, str]) -> None:
        """One evidence row per rating the run wrote, with its FULL hash.

        The trace prints ten hex characters of each rating's hash; the rest is
        recovered from the ReputationLedger's `rated` events (topic: the
        agent's id), matched by that prefix, from the ledger the run started at.
        """
        wanted: list[tuple[str, str, str | None, bool]] = []  # (prefix, agent_id, rating, landed)
        for line in lines:
            msg = str(line.get("msg") or "")
            m = RATED_LINE.match(msg)
            if m:
                wanted.append((m.group("prefix"), names.get(m.group("name"), m.group("name")), m.group("rating"), True))
                continue
            u = UNLANDED_LINE.match(msg)
            if u:
                wanted.append((u.group("prefix"), names.get(u.group("name"), u.group("name")), None, False))
        if not wanted:
            return
        chain = self.need_chain()
        # A resumed run that never recorded where it started scans the last
        # day of ledgers (~5 s each), well inside the RPC's retention window.
        start = self.state.start_ledger or max(1, chain.latest_ledger() - RESUME_SCAN_LEDGERS)
        try:
            events = [e for e in chain.events(self.contracts["reputation_ledger"], start) if e.topics[:1] == ["rated"]]
        except RpcError as exc:
            # Each rating below is then recorded as unresolved, with its prefix.
            self.note("poll", "events_unavailable", f"getEvents refused: {exc}", ledger=start)
            events = []
        for prefix, agent_id, rating, landed in wanted:
            match = next((e for e in events if e.tx_hash.startswith(prefix) and e.topics[1:2] == [agent_id]), None)
            if match is None:
                self.note(
                    "poll",
                    "rating_unresolved",
                    f"rating for {agent_id} (tx {prefix}…) not found in the ledger's rated events",
                    tx_prefix=prefix,
                    landed_per_trace=landed,
                    scanned_from_ledger=start,
                )
                continue
            seen = chain.observe(match.tx_hash, self.budgets.tx_observe)
            value = match.value if isinstance(match.value, list) else []
            self.tx(
                "poll",
                "rating",
                seen,
                contract=self.contracts["reputation_ledger"],
                agent=agent_id,
                summary=f"ReputationLedger.submit for {agent_id}: {rating or (value[0] if value else '?')}/100",
                rating=value[0] if value else rating,
                weight=value[1] if len(value) > 1 else None,
                kind=value[3] if len(value) > 3 else None,
            )

    # ── 5. verify ───────────────────────────────────────────────
    def read_settlement(self) -> dict[str, Any] | None:
        task_id = self.state.task_id
        assert task_id is not None
        try:
            listing = self.api.task_disputes(task_id, self.state.read_token, self.grant)
        except ApiError as exc:
            if exc.status != 404:
                raise
            listing = {}
        settlement = listing.get("settlement")
        if settlement:
            self.state.settlement = settlement
            self.save()
            return dict(settlement)
        return self.state.settlement

    def stage_verify(self) -> None:
        chain, buyer = self.need_chain(), self.need_buyer().public_key
        settlement = self.read_settlement()
        escrow = (self.state.authorize or {}).get("escrow") or self.contracts["payment_escrow"]
        version = self.state.escrow_version or 1
        if not settlement:
            why = (
                " Escrow v1 cannot settle an external payer's funds (D-039), so this is expected until v2 is deployed."
                if version == 1
                else ""
            )
            self.note("verify", "no_settlement", "the task has no settlement record" + why)
            raise Stop(
                EXIT_VERIFY_FAILED,
                "no settlement was recorded for this task, so there is nothing to verify or dispute." + why,
            )

        checks: list[verify.Check] = [
            verify.Check(
                "settlement_payer_is_buyer", settlement.get("payer") == buyer, f"payer {settlement.get('payer')}"
            )
        ]
        charge_tx = settlement.get("charge_tx")
        charge_seen = chain.observe(str(charge_tx), self.budgets.tx_observe) if charge_tx else None
        events: list[ChainEvent] = []
        if charge_seen is not None:
            self.tx(
                "verify",
                "settle" if version >= 2 else "charge",
                charge_seen,
                contract=escrow,
                amount=settlement.get("settled_usdc"),
                summary=f"PaymentEscrow v{version} {'settle' if version >= 2 else 'charge'} "
                f"for job {settlement.get('job_id_hex')}",
                job_id_hex=settlement.get("job_id_hex"),
            )
            if charge_seen.ledger:
                try:
                    events = chain.events(escrow, charge_seen.ledger, tx_hash=charge_seen.tx_hash)
                except RpcError as exc:
                    # A settle older than the RPC's event retention: the
                    # checks below then fail on missing events, and say why.
                    self.note("verify", "events_unavailable", f"getEvents refused: {exc}", ledger=charge_seen.ledger)

        receipts: list[str] | None = None
        if version >= 2:
            checks.append(verify.check_tx("v2_settle_tx", charge_seen))
            if self.state.owners is None:
                self.state.owners = self.owners_for([str(s.get("agent_id")) for s in settlement.get("steps") or []])
                self.save()
            charged, paid = verify.check_charged_events(events, settlement, self.state.owners)
            max_stroops = (self.state.authorize or {}).get("max_stroops")
            checks += [*charged, verify.check_settled_event(events, settlement, paid, max_stroops)]
            auth_id = (self.state.authorize or {}).get("auth_id_hex") or verify.settled_auth_id(events)
            if auth_id:
                checks.append(
                    verify.check_authorization_view(chain.escrow_authorization(escrow, auth_id, buyer), paid, buyer)
                )
            checks += self.balance_checks(events, paid, escrow)
            receipts = verify.charged_receipts(events)
            for e in events:
                if e.topics[:1] == ["charged"]:
                    self.say(f"    charged {e.topics[1:2]} {e.value}")
        else:
            checks += verify.check_v1_charge(charge_seen, events)

        proof_tx = settlement.get("proof_tx")
        proof_seen = chain.observe(str(proof_tx), self.budgets.tx_observe) if proof_tx else None
        checks.append(verify.check_tx("seal_tx", proof_seen))
        if proof_seen is not None:
            self.tx(
                "verify",
                "seal",
                proof_seen,
                contract=self.contracts["attestation_registry"],
                summary=f"AttestationRegistry.seal for job {settlement.get('job_id_hex')}",
                job_id_hex=settlement.get("job_id_hex"),
            )
        attestation = chain.attestation(
            self.contracts["attestation_registry"], str(settlement.get("job_id_hex")), buyer
        )
        checks += verify.check_seal(attestation, settlement, self.cfg.agent, receipts)

        ok = verify.passed(checks)
        for c in checks:
            mark = "PASS" if c.ok else ("n/a " if c.ok is None else "FAIL")
            self.say(f"    [{mark}] {c.name}: {c.detail}")
        self.note(
            "verify",
            "settlement_checks",
            f"escrow v{version}: {'all checks pass' if ok else 'CHECKS FAILED'}",
            escrow_version=version,
            checks=[{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
            attestation=attestation,
            steps=settlement.get("steps"),
        )
        if not ok:
            raise Stop(
                EXIT_VERIFY_FAILED, "the ledger does not show the settlement the API reports; see the checks above"
            )

    def balance_checks(self, events: list[ChainEvent], paid: int, escrow: str) -> list[verify.Check]:
        chain, buyer = self.need_chain(), self.need_buyer().public_key
        before = self.state.balances_before or {}
        owners = self.state.owners or {}
        sac = str(self.network.get("asset_sac") or "")
        addresses = list(dict.fromkeys([buyer, *owners.values()]))
        read = {a: chain.sac_balance(sac, a, buyer) for a in addresses} if before else {}
        after = {a: value for a, value in read.items() if value is not None}
        auth = self.state.authorize or {}
        fee = chain.fee_charged(str(auth["tx_hash"])) if auth.get("tx_hash") and before else None
        settler = self.settler(escrow)
        not_isolatable = {buyer, str(settler)} if settler else {buyer}
        self.note(
            "verify", "balances_after", f"{len(after)} balance(s) read after settle", balances=after, authorize_fee=fee
        )
        fee_in_asset = self.network.get("asset") in (None, "native")
        return [
            verify.check_buyer_delta(before.get(buyer), after.get(buyer), paid, fee, fee_in_asset),
            *verify.check_operator_deltas(before, after, owners, events, not_isolatable),
        ]

    def settler(self, escrow: str) -> str | None:
        """The settler, who pays the settle's fee and every refund: v2's own
        `settler()` view, else `/readiness` — which the frontend's proxy does
        not forward, so it is None through https://orizons.xyz."""
        try:
            return str(self.need_chain().simulate(escrow, "settler", [], self.need_buyer().public_key))
        except (SimulationError, RpcError):
            return ((self.api.readiness() or {}).get("ratings") or {}).get("signer")

    # ── 6. dispute ──────────────────────────────────────────────
    def stage_dispute(self) -> None:
        kp = self.need_buyer()
        settlement = self.read_settlement() or {}
        job = str(settlement.get("job_id_hex") or "")
        # The first --agent whose step delivered: a multi-agent run proving
        # that one agent failed still disputes the work that was paid for.
        step = next(
            (
                s
                for agent in self.cfg.all_agents
                for s in settlement.get("steps") or []
                if s.get("agent_id") == agent and s.get("delivered")
            ),
            None,
        )
        if not job or step is None:
            self.note("dispute", "nothing_to_dispute", f"no delivered step by {self.agents_label} in the settlement")
            raise Stop(EXIT_STAGE_FAILED, f"the settlement has no delivered step by {self.agents_label} to dispute")
        index = int(step["step_index"])

        existing = self.find_dispute(job, index)
        dispute = existing or self.open_dispute(kp, job, index)
        self.state.dispute = {
            "id": dispute["id"],
            "step_index": index,
            "agent_id": dispute.get("agent_id"),
            "status": dispute.get("status"),
        }
        self.save()
        self.note(
            "dispute",
            "dispute_existing" if existing else "dispute_opened",
            f"dispute {dispute['id']} on step {index} ({dispute.get('agent_id')}), status {dispute.get('status')}",
            dispute_id=dispute["id"],
            status=dispute.get("status"),
            step_index=index,
            charged_usdc=dispute.get("charged_usdc"),
            creditable_usdc=dispute.get("creditable_usdc"),
            job_id_hex=job,
        )
        self.say(f"  dispute {dispute['id']} ({dispute.get('status')}) on step {index}")
        self.take_read_grant()

    def find_dispute(self, job: str, index: int) -> dict[str, Any] | None:
        task_id = self.state.task_id
        if task_id is None:
            return None
        try:
            listing = self.api.task_disputes(task_id, self.state.read_token, self.grant)
        except ApiError:
            return None
        for d in listing.get("disputes") or []:
            if d.get("job_id_hex") == job and d.get("step_index") == index:
                return dict(d)
        return None

    def open_dispute(self, kp: Keypair, job: str, index: int) -> dict[str, Any]:
        """lib/disputes.ts raiseDispute: challenge → sign verbatim → open, with
        one retry on `challenge_expired` and nothing else retried."""
        for attempt in (1, 2):
            challenge = self.api.dispute_challenge(job, index)
            message, nonce = str(challenge["message"]), str(challenge["nonce"])
            check_dispute_message(message, job, index, nonce)
            signature = sign_message_b64(kp, message)
            self.console.redactor.register(signature)
            body = {
                "job_id_hex": job,
                "step_index": index,
                "reason": self.cfg.dispute_reason,
                "payer": kp.public_key,
                "nonce": nonce,
                "signature_b64": signature,
            }
            try:
                return self.api.open_dispute(body)
            except ApiError as exc:
                if attempt == 1 and exc.code == "challenge_expired":
                    continue
                self.note("dispute", "dispute_refused", f"{exc.status} {exc.code}: {exc.message}")
                raise Stop(EXIT_STAGE_FAILED, f"the dispute was refused: {exc}") from exc
            except UnknownOutcome as exc:
                # One dispute per step is the server's rule, so a read settles it.
                found = self.find_dispute(job, index)
                if found is not None:
                    return found
                self.note("dispute", "dispute_unknown", str(exc))
                raise Stop(
                    EXIT_UNKNOWN_OUTCOME, f"opening the dispute had an unknown outcome and none is listed: {exc}"
                ) from exc
        raise Stop(EXIT_STAGE_FAILED, "the dispute challenge expired twice")  # pragma: no cover

    def take_read_grant(self) -> None:
        """D-067: the payer signs a read challenge for a grant that reads the
        dispute's free text after the task token is gone. Best-effort — an
        older backend has no such route, and nothing downstream needs the text."""
        task_id, kp = self.state.task_id, self.need_buyer()
        if task_id is None:
            return
        try:
            challenge = self.api.read_challenge(task_id)
            message, nonce = str(challenge["message"]), str(challenge["nonce"])
            check_read_message(message, task_id, nonce)
            granted = self.api.read_grant(task_id, nonce, sign_message_b64(kp, message))
        except (ApiError, UnknownOutcome, SigningRefused) as exc:
            self.say(f"  no read grant ({type(exc).__name__}); continuing without the free text")
            return
        self.grant = str(granted["grant"])
        self.console.redactor.register(self.grant)

    # ── 7. uphold ───────────────────────────────────────────────
    def stage_uphold(self) -> None:
        dispute_id = str((self.state.dispute or {}).get("id") or "")
        current = self.api.dispute(dispute_id, grant=self.grant)
        status = current.get("status")
        if status == "credited":
            self.note("uphold", "uphold_skipped", f"dispute {dispute_id} is already credited", dispute_id=dispute_id)
            return
        if status != "open":
            self.note("uphold", "uphold_not_open", f"dispute {dispute_id} is {status}", dispute_id=dispute_id)
            raise Stop(
                EXIT_STAGE_FAILED if status == "rejected" else EXIT_UNKNOWN_OUTCOME,
                f"dispute {dispute_id} is {status!r}, not open. Not upheld again: an earlier adjudication "
                "left it here, and a blind re-uphold is how a buyer gets paid twice.",
            )
        assert self.adjudicator_key is not None
        try:
            upheld = self.api.uphold(dispute_id, self.adjudicator_key)
        except (UnknownOutcome, ApiError) as exc:
            after = self.api.dispute(dispute_id, grant=self.grant)
            unknown = isinstance(exc, UnknownOutcome)
            self.note(
                "uphold",
                "uphold_unknown" if unknown else "uphold_refused",
                f"{exc}; the dispute now reads {after.get('status')}",
                dispute_id=dispute_id,
                status=after.get("status"),
                refund_tx=after.get("refund_tx"),
            )
            if after.get("refund_tx"):
                self.tx(
                    "uphold",
                    "refund",
                    self.need_chain().observe(str(after["refund_tx"]), self.budgets.tx_observe),
                    contract=self.network.get("asset_sac"),
                    amount=after.get("credited_usdc") or after.get("creditable_usdc"),
                    summary="the credit the interrupted uphold left behind",
                )
            raise Stop(
                EXIT_UNKNOWN_OUTCOME if unknown else EXIT_STAGE_FAILED,
                f"uphold {'outcome unknown' if unknown else 'refused'} ({exc}); dispute reads {after.get('status')}. "
                "Not retried.",
            ) from exc
        self.state.dispute = {**(self.state.dispute or {}), "status": upheld.get("status")}
        self.save()
        self.say(f"  upheld: dispute {dispute_id} is {upheld.get('status')}")
        self.note(
            "uphold",
            "upheld",
            f"uphold answered {upheld.get('status')}",
            dispute_id=dispute_id,
            status=upheld.get("status"),
        )

    # ── 8. refund ───────────────────────────────────────────────
    def stage_refund(self) -> None:
        dispute_id = str((self.state.dispute or {}).get("id") or "")
        deadline = self.clock() + self.budgets.refund
        while True:
            d = self.api.dispute(dispute_id, grant=self.grant)
            rated = bool(d.get("rating_tx")) and d.get("rating_confirmed") is not False
            if d.get("status") == "credited" and rated:
                break
            if d.get("status") == "upheld":
                self.note(
                    "refund",
                    "refund_failed",
                    "the credit failed on the ledger; the dispute is upheld and payable",
                    dispute_id=dispute_id,
                )
                raise Stop(EXIT_STAGE_FAILED, f"dispute {dispute_id} is upheld but not credited: the transfer failed")
            if self.clock() + self.budgets.refund_interval > deadline:
                self.note(
                    "refund",
                    "refund_timeout",
                    f"dispute still {d.get('status')}",
                    status=d.get("status"),
                    refund_tx=d.get("refund_tx"),
                    rating_tx=d.get("rating_tx"),
                )
                hint = (
                    " The buyer is paid; the rating did not land. Upholding again retries the rating alone "
                    "(the ledger's replay guard makes it safe) — do it deliberately, with --from-dispute."
                    if d.get("status") == "credited"
                    else ""
                )
                raise Stop(
                    EXIT_TIMED_OUT, f"dispute {dispute_id} is {d.get('status')} after {self.budgets.refund:.0f}s.{hint}"
                )
            self.sleep(self.budgets.refund_interval)

        chain = self.need_chain()
        self.tx(
            "refund",
            "refund",
            chain.observe(str(d["refund_tx"]), self.budgets.tx_observe),
            contract=self.network.get("asset_sac"),
            agent=d.get("agent_id"),
            amount=d.get("credited_usdc") if d.get("credited_usdc") is not None else d.get("creditable_usdc"),
            summary="settler → buyer credit for the upheld dispute (ADR 0002: platform-funded)",
            dispute_id=dispute_id,
        )
        self.tx(
            "refund",
            "dispute_rating",
            chain.observe(str(d["rating_tx"]), self.budgets.tx_observe),
            contract=self.contracts.get("reputation_ledger"),
            agent=d.get("agent_id"),
            summary="the dispute-kind rating on the ReputationLedger",
            dispute_id=dispute_id,
            rating_confirmed=d.get("rating_confirmed"),
        )
        self.snapshot("after_dispute")

    # ── 9. reputation ───────────────────────────────────────────
    def stage_reputation(self) -> None:
        final = self.snapshot("final")
        snaps = [
            r["detail"]
            for r in self.log.rows()
            if r.get("event") == "reputation_snapshot"
            and r.get("run_id") == self.state.run_id
            and r.get("detail", {}).get("smoothed_bps") is not None
        ]
        labels = [s.get("label") for s in snaps]
        scores = [(s.get("label"), s.get("smoothed_bps"), s.get("lower_bound_bps"), s.get("source")) for s in snaps]
        by_label = {s.get("label"): s for s in snaps}
        start = by_label.get("start") or (snaps[0] if snaps else None)
        rated = [s for s in snaps if str(s.get("label", "")).startswith("after_rating")] or [
            by_label.get("after_ratings")
        ]
        after_ratings = rated[-1] if rated and rated[-1] else None
        after_dispute = by_label.get("after_dispute")

        def moved(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool | None:
            if a is None or b is None:
                return None
            return (a.get("smoothed_bps"), a.get("lower_bound_bps"), a.get("count")) != (
                b.get("smoothed_bps"),
                b.get("lower_bound_bps"),
                b.get("count"),
            )

        summary = {
            "labels": labels,
            "moved_start_to_ratings": moved(start, after_ratings),
            "moved_ratings_to_dispute": moved(after_ratings, after_dispute),
            "fell_after_dispute": (
                None
                if after_ratings is None or after_dispute is None
                else int(after_dispute.get("smoothed_bps") or 0) < int(after_ratings.get("smoothed_bps") or 0)
            ),
            "source_start": start.get("source") if start else None,
            "source_final": final.get("source") if final else None,
            "scores": scores,
        }
        self.note(
            "reputation",
            "reputation_summary",
            f"score moved {summary['moved_start_to_ratings']} then {summary['moved_ratings_to_dispute']}",
            **summary,
        )
        self.say(f"  reputation across the run: {scores}")
        self.say(
            f"  moved after ratings: {summary['moved_start_to_ratings']}"
            f" · after dispute: {summary['moved_ratings_to_dispute']}"
            f" · fell: {summary['fell_after_dispute']} · source {summary['source_start']} -> {summary['source_final']}"
        )
