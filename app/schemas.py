from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

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


# ───── Tasks ───────────────────────────────────────────────
TaskStatus = Literal["pending", "running", "complete", "failed"]


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
    # The designated agent this step replaced, when the reputation floor forced
    # a substitution on the kit path. None on the normal path. Lets the plan
    # card badge the step inline without re-joining the response notices.
    substituted_for: str | None = None
    # True when this step was re-admitted below the routing floor by the
    # starvation backstop — kept so the plan stays workable, but flagged so the
    # buyer sees it is a degraded choice. Inline mate to substituted_for.
    degraded: bool = False


class Plan(BaseModel):
    steps: list[PlanStep]


class StoredPlan(BaseModel):
    id: str
    intent: str
    plan: Plan
    total_usdc: float
    total_eta: float


# Why the floor acted on an agent — a CLOSED set, because the plan card renders
# one sentence per value and the integration guide documents them; a free-text
# reason is unrenderable and undocumentable.
#
# Two values story 3.02 asked for are deliberately absent:
#
#   * `inactive` — `AgentRegistry.set_active(id, false)` syncs to
#     `Agent.status == "offline"`, but nothing in routing reads that field (its
#     only consumer is a metrics counter). An agent is never excluded for being
#     inactive, so shipping the value would put a state in the API contract that
#     the system cannot produce.
#   * `not_selected_by_planner` — the story's own product rules forbid listing
#     every unpicked agent, which would drown the signal this exists to create.
#     A plan that simply did not choose an agent is not an exclusion.
ExclusionReason = Literal["below_floor", "unbound_endpoint", "floor_relaxed"]


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


# ───── Trace ───────────────────────────────────────────────
TraceLevel = Literal["input", "exec", "proof", "cost", "out", "error", "artifact"]


class TraceLine(BaseModel):
    t: str
    level: TraceLevel
    msg: str


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
class OverviewMetrics(BaseModel):
    agents_online: int
    tasks_per_sec: float
    avg_completion: float  # 0..1
    avg_trust: float  # 0..5
    throughput: list[int]  # sparkline
    skills: list[dict[str, Any]]  # [{name, pct, tone}]


# ───── Requests ────────────────────────────────────────────
class DecomposeRequest(BaseModel):
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
