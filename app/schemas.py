from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .llm.tiers import Tier

# Agent ids are contract Symbols: short alphanumeric/underscore tokens. Reject
# garbage at the router edge instead of paying an RPC round-trip to find out.
# Lives here, not in a router, because more than one router now bounds an agent
# id with it — anything outside this charset could only ever fail on-chain.
# NOTE: this is NOT the same rule as HeaderSafeStr's charset below, which
# additionally allows `.` and `-` and is driven by CRLF header safety.
AGENT_ID_PATTERN = r"^[A-Za-z0-9_]{1,32}$"

# ───── Registry ────────────────────────────────────────────
AgentStatus = Literal["online", "idle", "offline"]
# Provenance is contracted evidence, not cosmetics: SOW §6.3's "≥ 2 externally
# operated agents" must be provable from the API, so every agent carries where
# it came from (story 1.02).
AgentSource = Literal["seeded", "onchain"]


class Agent(BaseModel):
    id: str
    name: str
    skills: list[str]
    price: float
    rep: float
    status: AgentStatus
    runs: int
    real: bool = False  # whether backed by a real Agno Agent
    # Registering wallet (G...) — populated for on-chain indexed agents,
    # None for the seeded catalog. Story 1.08's operator view filters on it.
    owner: str | None = None
    source: AgentSource = "seeded"
    # Whether an endpoint is bound — the marketplace's "is this operational"
    # signal (story 3.05), answered for every agent in one list read rather
    # than one HTTP call per row against a shared rate-limit bucket.
    #
    # Tri-state on purpose. `None` means the question does not apply: a seeded
    # agent runs on a worker inside this process and has no endpoint to bind,
    # so reporting `False` would describe a defect where there is none — the
    # same conflation `binding_status.needsBinding` exists to prevent on the
    # client. Only an on-chain agent can be meaningfully bound or unbound.
    bound: bool | None = None


# ───── Tasks ───────────────────────────────────────────────
TaskStatus = Literal["pending", "running", "complete", "failed"]

# What happened to a paid run's money, as a field a client can branch on rather
# than a trace sentence it has to parse. `status` cannot say it: a run whose
# settlement failed still finalizes `complete` when it delivered (ADR 0010).
#   settled      the escrow paid out and the transaction CONFIRMED
#   released     v2 only: nothing was delivered, so an empty `settle` returned
#                the buyer's whole custody and paid nobody
#   skipped      v1 only: nothing was delivered, so nothing was charged and
#                the authorization was left as it was
#   unconfirmed  submitted and then lost track of: it MAY still land, and it is
#                never retried (see `execution_svc._settle_onchain`)
#   failed       definitely did not move money: refused before it was sent, or
#                rejected by the ledger
# None on a task that asked for no on-chain settlement (a simulated run).
SettlementState = Literal["settled", "released", "skipped", "unconfirmed", "failed"]

# What became of a paid run's attestation seal (AttestationRegistry.seal), for
# the same reason `settlement` exists: a client branches on a field, not on a
# trace sentence. Set only once a seal was submitted, so never on a simulated
# run or on one that paid nobody (nothing to attest, D-086).
#   sealed       the attestation is on the ledger; `proof_tx` is its hash when
#                the transaction that wrote it is known
#   pending      submitted and not yet confirmed: the run is reconciling it
#                (`execution_svc._reconcile_seal`), re-checking by hash and by
#                job, and re-submitting only once it provably is not there
#   unconfirmed  reconciliation ran out of time without an answer either way:
#                it MAY still be on the ledger
#   failed       provably not on the ledger, every bounded re-submission
#                included — the job's payment stands, unattested
SealState = Literal["sealed", "pending", "unconfirmed", "failed"]

# What a seal attests to, so a client can label it. Set with `seal`, from the
# moment it is submitted.
#   paid           the agents that delivered AND were paid, one per payout,
#                  each beside its payout's receipt, with the total that moved
#   delivery_only  nobody could be paid (no confirmed on-chain owner, a free
#                  step, an authorization already spent): the agents that
#                  DELIVERED, with no receipt and a total of zero — on-chain,
#                  nothing in it reads as a payment
SealKind = Literal["paid", "delivery_only"]


def humanize_age(seconds: float) -> str:
    """Coarse relative age, e.g. 125.0 → "2m ago". Clock skew reads "just now"."""
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3_600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86_400:
        return f"{int(seconds // 3_600)}h ago"
    return f"{int(seconds // 86_400)}d ago"


class TaskSummary(BaseModel):
    """A task without its artifact — the shape the task list is served in.

    An artifact carries `files[*].content` plus a byte-identical `preview_html`
    (30–38 KB for a baked kit), so a completed task is ~70 KB. The dashboard
    polls the list every 5s and reads none of it, which made a full list ~1.4 MB
    per poll per open tab. Listing summaries keeps the poll cheap; the payload
    is served by GET /tasks/{id}/artifact, the one place it is actually read.
    """

    id: str
    intent: str
    agents: int
    spent: float
    status: TaskStatus
    # Unix epoch seconds — the machine-readable truth, and what a client should
    # format itself. Pre-rendering a relative string server-side is what froze
    # the old `started` field at "just now": it was written once at creation and
    # nothing ever recomputed it, so hours-old rows still claimed to be seconds
    # old. `started` below stays in the payload, unchanged in type, because the
    # dashboard renders it verbatim (`started: string` in lib/types.ts) — but it
    # is now derived, never stored, so it cannot go stale again.
    started_at: float = Field(default_factory=time.time)
    charge_tx: str | None = None
    proof_tx: str | None = None
    settlement: SettlementState | None = None
    seal: SealState | None = None
    seal_kind: SealKind | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def started(self) -> str:
        """Human-readable age ("2m ago"), computed at serialization time."""
        return humanize_age(time.time() - self.started_at)


class Task(TaskSummary):
    """A stored task, artifact included. This is what app state holds and what
    the per-task routes serve; only the list route narrows to TaskSummary."""

    artifact: dict | None = None


# ───── Artifacts ──────────────────────────────────────────
class ArtifactFile(BaseModel):
    path: str
    language: str
    content: str


class CodeArtifact(BaseModel):
    title: str
    summary: str
    files: list[ArtifactFile]
    entry: str
    preview_html: str


# ───── Plans ───────────────────────────────────────────────
class PlanStep(BaseModel):
    agent_id: str = Field(..., description="Must match a registered agent id")
    agent_name: str | None = None  # backfilled server-side
    rationale: str = Field(..., description="<= 20 words")
    est_price_usdc: float = Field(..., ge=0)
    est_eta_seconds: float = Field(..., ge=0)
    rep_bps: int | None = None  # smoothed reputation at plan time (0..10_000)
    rep_source: Literal["onchain", "prior"] | None = None
    # The conservative bound the routing floor is judged on — `rep_bps` is the
    # headline score, this is the number that decided whether the agent was
    # routable. Carried so a card can show both without a second request.
    rep_lower_bound_bps: int | None = None
    # Lifetime rating count. 0 with `rep_source == "prior"` and `rep_degraded`
    # False is a genuine cold start — a newcomer with no history, not an agent
    # with a bad one. A failed read also serves the prior with a 0 here, which
    # is why the pair alone cannot say it.
    rep_count: int | None = None
    # Share of those ratings that were disputes, in bps (0..10_000).
    rep_dispute_rate_bps: int | None = None
    # True when this agent's on-chain reputation read FAILED and the Bayesian
    # prior was served in its place, so every `rep_*` number above is an
    # estimate. NOT `degraded` below: that one means the step was re-admitted
    # below the floor — a verdict that was reached, where this says one could
    # not be. Per-step mate to `DecomposeResponse.reputation_degraded`.
    rep_degraded: bool = False
    # The designated agent this step replaced, when the reputation floor forced
    # a substitution on the kit path. None on the normal path. Lets the plan
    # card badge the step inline without re-joining the response notices.
    substituted_for: str | None = None
    # True when this step was re-admitted below the routing floor by the
    # starvation backstop — kept so the plan stays workable, but flagged so the
    # buyer sees it is a degraded choice. Inline mate to substituted_for.
    degraded: bool = False
    # How hard this step is, which decides the model a built-in worker runs it
    # on (low → Haiku, moderate → Sonnet, complex → Opus; `models.tiers` on the
    # decompose response names the exact ids). Never above the plan's own
    # tier. None on a plan built by the legacy planner, which has no tiers.
    tier: Tier | None = None


class Plan(BaseModel):
    steps: list[PlanStep]
    # The request's overall complexity as the request check judged it. None
    # on a plan built by the legacy planner.
    tier: Tier | None = None


class StoredPlan(BaseModel):
    id: str
    intent: str
    plan: Plan
    total_usdc: float
    total_eta: float
    # Unix epoch seconds, like `TaskSummary.started_at`. `/execute` refuses a
    # plan older than its TTL (`execution_svc.PLAN_TTL_SECONDS`): the card the
    # buyer authorised stamps prices, reputation and notices at this instant,
    # and without a clock a plan stayed executable until 200 newer ones pushed
    # it out of the store — hours, on a quiet deployment.
    created_at: float = Field(default_factory=time.time)
    # What the buyer was TOLD when they authorised this plan — the same four
    # plan-level facts `DecomposeResponse` carries, kept so `/execute` and any
    # later read can tell a plan judged on estimates, served as a fallback or
    # built with the floor relaxed from one that was not. Dropping them left
    # the stored plan claiming nothing about how it was built. Defaults are the
    # "nothing to report" values, so a plan built without them validates.
    notices: list[PlanFloorNotice] = Field(default_factory=list)
    # None, not DecomposeResponse's 0: a stored plan that does not record the
    # floor it was judged against has no floor to report, and 0 would read as
    # "judged against a floor of zero".
    floor_bps: int | None = None
    reputation_degraded: bool = False
    planner_fallback: bool = False


# Why the floor acted on an agent — a CLOSED set, because the plan card renders
# one sentence per value and the integration guide documents them; a free-text
# reason is unrenderable and undocumentable.
#
# Two values story 3.02 asked for are deliberately absent:
#
#   * `inactive` — `AgentRegistry.set_active(id, false)` syncs to
#     `Agent.status == "offline"`, and routing does read that field
#     (`orchestrator_svc._is_listed`): a delisted agent is never offered to the
#     planner, kept by the clamp, promoted as a substitute or re-admitted by a
#     backstop. It is still not a reason code, because a withdrawal is not a
#     verdict the floor reached — it is the operator's own decision, already
#     visible on their `GET /api/agents` row — so a delisted agent gets no
#     notice at all (argued in `orchestrator_svc._routable_registry`).
#   * `not_selected_by_planner` — the story's own product rules forbid listing
#     every unpicked agent, which would drown the signal this exists to create.
#     A plan that simply did not choose an agent is not an exclusion.
#
# `unreachable_endpoint` (D-084) is the one value added since: a bound agent
# whose endpoint failed its latest health check, left out of the plan while
# that failure is fresh (`app/services/reachability.py`). Appended, so the
# existing three keep their positions for any client that indexes them.
ExclusionReason = Literal["below_floor", "unbound_endpoint", "floor_relaxed", "unreachable_endpoint"]


class PlanFloorNotice(BaseModel):
    """One reputation-floor action taken while building a plan.

    Surfaced on DecomposeResponse so the buyer sees why a curated pipeline
    changed shape rather than a silently reshuffled plan — story 3.02 renders
    these. Additive with a safe default; clients that ignore it are unaffected.
    """

    # `kind` is what happened to the PLAN; `reason_code` is why. They are
    # orthogonal, not competing vocabularies: an agent can be excluded for
    # being below the floor or for having no endpoint, and both read as
    # kind="excluded". `kind` is not renamed because the plan card already
    # ships against it.
    kind: Literal["excluded", "substituted", "degraded"]
    agent_id: str  # the designated kit agent the floor acted on
    agent_name: str | None = None
    replacement_id: str | None = None  # the substitute, when kind == "substituted"
    replacement_name: str | None = None
    reason: str  # e.g. "below routing floor (4200 < 5500 bps)"
    # Additive with a default so a notice built before this field existed still
    # validates; every notice this codebase constructs sets it explicitly.
    reason_code: ExclusionReason = "below_floor"
    # The deciding numbers, as data rather than interpolated into `reason`. A
    # client that wants to render "4.10 against a 3.00 floor" should not have to
    # parse an English sentence to get there.
    lower_bound_bps: int | None = None  # None when the agent had no rep entry
    floor_bps: int = 0
    # The evidence behind that bound: how many ratings it rests on, and what
    # share of them were disputes. Without them a buyer cannot tell an agent
    # the floor excluded for upheld disputes from one that is merely new and
    # unlucky — both read "below routing floor". Reported, not routed on:
    # whether disputes should weigh more, or carry their own floor, is an open
    # product decision. None when there is no rep entry (unbound, or absent).
    count: int | None = None
    dispute_rate_bps: int | None = None
    # True when the floor acted because a rating landed since this agent's last
    # reputation read and the fresh read has not answered yet. `lower_bound_bps`
    # is then the PRE-rating value and can sit above `floor_bps`, so a renderer
    # must show `reason` rather than "lower bound against the floor".
    # `reason_code` stays `below_floor` (the floor's verdict), which is why this
    # is a separate flag and not a new code a client's closed union would lack.
    awaiting_fresh_read: bool = False


# ───── Trace ───────────────────────────────────────────────
TraceLevel = Literal["input", "exec", "proof", "cost", "out", "error", "artifact"]


class TraceLine(BaseModel):
    t: str
    level: TraceLevel
    msg: str
    # Set on the one line that reports a paid run's settlement outcome, and on
    # no other, so a trace reader finds it without parsing `msg`.
    settlement: SettlementState | None = None


# ───── Flow ────────────────────────────────────────────────
class FlowNode(BaseModel):
    id: str
    label: str
    sub: str
    x: float
    y: float


class Flow(BaseModel):
    nodes: list[FlowNode]
    edges: list[tuple[str, str]]


# ───── Metrics ─────────────────────────────────────────────
# GET /api/metrics/overview. Every number is measured; a part that could not be
# read is null (or []) and sets `degraded`, never a stand-in value. The
# frontend codes against this shape (docs/decisions/0013-overview-measured-only.md).
class OverviewAgents(BaseModel):
    registered: int  # every agent GET /api/agents lists
    onchain: int  # source == "onchain": registered on the AgentRegistry
    seeded: int  # source == "seeded": the platform's own catalog
    # On-chain agents whose owner is outside operator, by adoption_svc's rule.
    # None only when the rule itself could not be built.
    external: int | None
    # On-chain agents with an endpoint bound. None while the bound set has not
    # been loaded, because an unloaded set says nothing about any agent.
    bound: int | None
    online: int  # status == "online"


class OverviewOperators(BaseModel):
    external_wallets: int | None  # distinct owners of the external agents


class SettledDay(BaseModel):
    date: str  # UTC calendar day, YYYY-MM-DD
    settled: int


class OverviewWorkflows(BaseModel):
    settled: int | None  # settled workflows in the durable settlement store
    series: list[SettledDay]  # the last 14 UTC days, oldest first; [] if unreadable


class OverviewTasks(BaseModel):
    recent: int  # tasks held by the in-memory task store
    complete: int
    failed: int
    completion_rate: float | None  # complete / (complete + failed); None with no terminal task


class OverviewTrust(BaseModel):
    avg: float | None  # mean smoothed on-chain reputation, 0..5; None with no on-chain evidence
    rated_agents: int | None  # agents with on-chain rating evidence; None if the read failed


class SkillShare(BaseModel):
    name: str  # a skill, or "other" for everything outside the top five
    agents: int  # agents carrying it
    pct: int  # share of all skill tags; the list sums to 100


class OverviewMetrics(BaseModel):
    generated_at: float  # epoch seconds the numbers were computed at
    agents: OverviewAgents
    operators: OverviewOperators
    workflows: OverviewWorkflows
    tasks: OverviewTasks
    trust: OverviewTrust
    skills: list[SkillShare]
    # Whether the on-chain registry mirror had finished a full pass when these
    # numbers were computed. False after a restart while the mirror is still
    # filling: the agent counts (and the skill mix) are then a prefix of the
    # registry, served as measured but partial, and `degraded` is true.
    registry_synced: bool
    degraded: bool  # True when any part above could not be fully read, or the registry is not synced


# ───── Requests ────────────────────────────────────────────
class DecomposeRequest(BaseModel):
    # Stripped BEFORE the length bounds apply, so whitespace can neither make
    # up the three characters nor count toward the 500. A blank intent used to
    # pass and was sent to the planner as one paid LLM call about nothing.
    model_config = ConfigDict(str_strip_whitespace=True)

    intent: str = Field(..., min_length=3, max_length=500)


class DecomposeResponse(BaseModel):
    plan_id: str
    intent: str
    steps: list[PlanStep]
    total_usdc: float
    total_eta: float
    # Reputation-floor actions taken while building this plan (exclusions,
    # substitutions, starvation-backstop degradations). Empty on the common
    # path where every routed agent clears the floor.
    notices: list[PlanFloorNotice] = Field(default_factory=list)
    # The floor actually applied to THIS plan, so the card can state the
    # threshold rather than only the verdict. Read from settings at plan time,
    # not assumed by the client: the value is configurable per deployment and a
    # client that hardcoded it would narrate the wrong number after a change.
    floor_bps: int = 0
    # At least one reputation read in this plan's snapshot fell back to the
    # Bayesian prior because the ledger was unreadable. The buyer is being sold
    # a trust signal computed from an estimate, and has a right to know before
    # they authorize payment.
    #
    # Deliberately NOT named `degraded`: that word already means "re-admitted
    # below the floor by the starvation backstop" on both `PlanStep` and
    # `PlanFloorNotice.kind`, and a third meaning in one payload is a defect
    # waiting to be written.
    reputation_degraded: bool = False
    # True when `steps` is the deterministic fallback plan rather than the
    # planner's own: the planning model failed or answered with something that
    # is not a plan, or every step it chose was clamped away. The fallback is
    # still a stored, executable plan — one step, drawn from the same shortlist
    # under the same floor — but a fixed rule picked it, not a reading of this
    # intent, and the buyer should know that before paying for it. Why the
    # planner failed is logged, never returned. Always False on the demo-kit
    # path, which never asks the planner anything.
    planner_fallback: bool = False


class ExecuteRequest(BaseModel):
    plan_id: str = Field(..., max_length=64)
    auth_id_hex: str | None = Field(
        default=None, pattern=r"^[0-9a-fA-F]{32}$"
    )  # 32-hex auth id from PaymentEscrow.authorize
    payer: str | None = Field(default=None, pattern=r"^G[A-Z2-7]{55}$")  # G... address of the payer (from Freighter)


class ExecuteResponse(BaseModel):
    task_id: str
    # Capability token for reading this task (status/artifact/trace). Always
    # returned so clients can store it before TASK_AUTH_REQUIRED is flipped
    # on; harmless while enforcement is off.
    read_token: str | None = None


class X402Request(BaseModel):
    # agent_id is echoed into the X-Orizon-Payment-Required response header —
    # restrict it to a safe token so header injection (CR/LF) is impossible.
    agent_id: str = Field(..., max_length=64, pattern=r"^[A-Za-z0-9_.\-]{1,64}$")
    amount_usdc: float = Field(..., gt=0, le=10_000, allow_inf_nan=False)


class X402Response(BaseModel):
    status: Literal["402", "paid"]
    receipt: str | None = None
