from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..agents.registry import get_worker
from ..agents.workers.prompt_safety import fence_untrusted, sanitize_untrusted
from ..config import settings
from ..demo_kits import detect_kit
from ..schemas import PlanStep, SettlementState, StoredPlan, Task, TaskStatus, TraceLevel, TraceLine
from ..security import CodedHTTPException
from ..state import state
from ..trace_bus import bus
from . import failure_tracker, rating_writer, reputation_svc, task_persistence
from .binding_registry import resolve_worker
from .dispute_store import OUTPUT_SUMMARY_MAX_CHARS, SettlementRecord, SettlementStep, get_dispute_store
from .orchestrator_svc import _is_listed

logger = logging.getLogger(__name__)

# Per-step ceiling — gpt-5.3-codex producing a 600–1000 line artifact can
# legitimately take 30–90s. Enough headroom, still bounded.
STEP_TIMEOUT_SECONDS = 120.0

# Strong references to in-flight background runs — asyncio.create_task alone
# only keeps a weak reference, so an un-referenced task can be garbage
# collected mid-run. Tasks remove themselves on completion.
_background_tasks: set[asyncio.Task] = set()


class CapacityExhaustedError(RuntimeError):
    """execute_plan refused to start: the concurrent-workflow ceiling
    (settings.orchestrator_max_concurrent) is already in flight. The router
    maps this to HTTP 503 "capacity_exhausted"."""


class PlanExpiredError(CodedHTTPException):
    """execute_plan refused a stored plan older than `settings.plan_ttl_seconds`.

    An HTTP exception rather than a bare RuntimeError like its sibling above so
    that `/execute` answers 410 `plan_expired` in the unified envelope without
    the router having to learn it — the router belongs to another lane, and an
    unmapped domain error would surface as a 500. Raised before any task is
    minted, so nothing runs and nothing is charged. The message names no
    configured limit, per `CodedHTTPException`'s disclosure rule.
    """

    def __init__(self, plan_id: str) -> None:
        super().__init__(
            410,
            "plan_expired",
            "this plan is too old to execute — build a fresh plan from the same intent and authorise that one",
        )
        self.plan_id = plan_id


def _wall_clock() -> float:
    """`time.time`, behind a seam the expiry tests can pin."""
    return time.time()


def plan_expired(plan: StoredPlan, now: float | None = None) -> bool:
    """True once `plan` is strictly older than `settings.plan_ttl_seconds`.

    A plan exactly TTL old still executes: the bound is inclusive, so the
    number means "executable for this long", not "one tick less".
    """
    age = (_wall_clock() if now is None else now) - plan.created_at
    return age > settings.plan_ttl_seconds


def _track_background_task(task: asyncio.Task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_on_background_task_done)


def _on_background_task_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("background execution task failed: %s", exc, exc_info=exc)


def _now_ts(start: float) -> str:
    elapsed = time.monotonic() - start
    seconds = int(elapsed)
    hundredths = int((elapsed - seconds) * 1000)
    return f"{seconds:02d}.{hundredths:03d}"


async def _emit(
    task_id: str,
    start: float,
    level: TraceLevel,
    msg: str,
    *,
    settlement: SettlementState | None = None,
) -> TraceLine:
    """Append one trace line and publish it.

    `settlement` marks the line that reports a paid run's settlement outcome,
    and is written onto the task in the same breath, so the trace and the task
    can never disagree about it (ADR 0010).
    """
    if settlement is not None:
        task = state.tasks.get(task_id)
        if task is not None:
            state.put_task(task.model_copy(update={"settlement": settlement}))
    line = TraceLine(t=_now_ts(start), level=level, msg=msg, settlement=settlement)
    state.append_trace(task_id, line)
    await bus.publish(task_id, line)
    return line


def _unusable_field(output: dict) -> str | None:
    """The first field of a worker's output whose SHAPE the post-step handling
    below cannot consume, or None when every field of it is usable.

    Since story 2.01 a step's output can be whatever JSON an operator's
    endpoint chose to return, and the handling that follows joins the critic
    lists, reads `artifact` as a mapping and walks `artifact.files` — each of
    which raises on the wrong type, and none of which sits inside the per-step
    try/except. One hostile field would therefore reach the run-level handler:
    the whole workflow finalizes as "failed" and the on-chain settlement that
    pays every OTHER agent in the plan never runs. Proving the shape here, one
    branch below the non-dict check and before the step is billed, keeps a bad
    envelope exactly what it is — that STEP's failure.

    Absent and falsy values are usable: every reader below already guards for
    them (`or []`, `if art:`). Only a present value of the wrong type is not.
    """
    for key in ("critic_violations", "critic_notes"):
        value = output.get(key)
        if value and not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
            return key
    art = output.get("artifact")
    if not art:
        return None
    if not isinstance(art, dict):
        return "artifact"
    files = art.get("files", [])
    if not isinstance(files, list):
        return "artifact.files"
    if any(not isinstance(f, dict) or not isinstance(f.get("content", ""), str) for f in files):
        return "artifact.files"
    return None


def _rating_view(output: dict, *, first_party: bool) -> dict[str, Any]:
    """The record of one step's output that the settler's rating reads.

    reputation_svc.synthetic_rating awards a flat 95 to output marked
    `source="baked"` — deterministic, pre-validated kit output produced by a
    worker in this repo. `source` is ALSO a field the published external
    envelope lets an operator set, so with the rating lookup now finding
    external output that branch would be self-dealing: a one-line response
    buys a permanent 95/100 on-chain. A worker that is not the first-party one
    registered for this agent id therefore does not get to supply it. Every
    other signal the rating reads (did it deliver, did it ship an artifact,
    what did the critic find) is checkable workflow evidence and passes
    through untouched.
    """
    if first_party:
        return output
    return {k: v for k, v in output.items() if k != "source"}


# Uppercase, and long enough that prompt_safety's marker-forgery redaction
# (`BEGIN|END <LABEL>` with LABEL ≥ 4 chars) covers a payload that tries to
# spell out the end of its own block.
_OPERATOR_FENCE_LABEL = "OPERATOR_OUTPUT"

# The operator-written prose of the external envelope — the fields whose only
# purpose is to be read. `artifact` and `preview_url` are deliberately absent;
# see the docstring below.
_OPERATOR_PROSE_FIELDS = ("summary", "critic_violations", "critic_notes")


def _fenced_for_context(output: dict) -> dict[str, Any]:
    """`output` with its operator-written prose fenced, for `context` only.

    Story 2.02's AC-5 and Product Rule 5 require operator output to be fenced
    before it can reach an LLM step, and nothing fenced it. The outcome held
    anyway — but only because `external.{agent_id}` is a key no worker reads,
    which is a property of the READERS, and the readers are the part most
    likely to change. One line of the form `context[worker.name] = summary`
    in a future worker reopens it silently, with no test failing. Fencing
    where the value ENTERS context makes it a property of the value instead,
    so a later reader inherits the defence rather than having to remember it.

    `fence_untrusted`, not `fence_user_input`: the latter labels its block
    USER_INPUT and clamps at MAX_INTENT_CHARS = 500, which would silently
    truncate a legitimate 2 000-char summary (ADR 0004 corrects the card here).

    A NEW dict, and only the context copy. The rating view, the artifact handed
    back to the buyer and every trace line read the raw `output`, so the fence
    cannot change what an agent is scored on or what the buyer receives.

    The artifact is NOT fenced. It is a deliverable rather than prose: it
    reaches the viewer through its own hardening path (`harden_artifact` plus
    the sandboxed iframe, ADR 0004 D1), and a worker that read a fenced copy
    out of context and re-emitted it as its own output would ship the security
    directive into the buyer's artifact. The one worker that already splices
    another step's artifact into a prompt — `code_critic` — fences it at the
    prompt site, which is where a 120 kB blob should be fenced once rather
    than carried fenced. `preview_url` is excluded for the plainer reason that
    fencing a URL stops it being one; it is already bounded to an http(s)
    string by the external contract and by `_trace_url`.
    """
    fenced: dict[str, Any] = dict(output)
    for key in _OPERATOR_PROSE_FIELDS:
        value = fenced.get(key)
        if isinstance(value, str):
            fenced[key] = fence_untrusted(value, label=_OPERATOR_FENCE_LABEL)
        elif isinstance(value, list):
            # Per item, not one joined block: the readers of these two keys
            # take a list (`or []`, then join), so collapsing them to a string
            # would break the very shape `_unusable_field` proved one branch
            # earlier. That gate also guarantees every item here is a `str`.
            fenced[key] = [fence_untrusted(item, label=_OPERATOR_FENCE_LABEL) for item in value]
    return fenced


def _trace_url(value: object) -> str | None:
    """An operator-supplied preview URL, when it is safe to put in a trace line.

    The URL is chosen by whoever ran the step, and the line it lands in is
    rendered in the buyer's viewer and kept with the task, so it is surfaced
    only as a string carrying an http(s) scheme — a `javascript:` or `data:`
    link is refused outright — and held to the same 180-char ceiling every
    other traced value already gets.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    return url[:180]


def _summarize(output: dict) -> str:
    if "summary" in output:
        return str(output["summary"])[:180]
    counts = output.get("counts")
    if isinstance(counts, dict):
        return ", ".join(f"{k}={v}" for k, v in counts.items())
    return "done"


def _stored_summary(task_id: str, step_index: int, summary: str) -> str | None:
    """A delivered step's trace summary as its settlement keeps it (story 4.05).

    `summary` is the text of the step's `out` trace line — the line the buyer
    watched — so the dispute form shows what the trace showed rather than a
    second rendering of the output that could disagree with it. It is still
    untrusted, an external agent's own words, and it outlives the trace: it is
    read back into an API response and shown in the console for the whole
    dispute window. So it is cleaned with `sanitize_untrusted`, the primitive
    `dispute_svc` already uses for the buyer's reason, which blanks the control
    characters that would forge structure nobody wrote there.

    Bounded at the store's OUTPUT_SUMMARY_MAX_CHARS, not trusted to
    `_summarize`'s 180. That cap is a trace-formatting choice covering only the
    `summary` branch — the `counts` branch joins every entry with no limit —
    and it can change for trace reasons without anyone thinking of the
    settlement row. The store's constant is the one that states what a row may
    hold, so it is the one a writer cleans to.

    Never raises. This runs inside the run loop, where an exception reaches the
    run-level handler: the workflow would finalize as "failed" and the charge
    that pays every agent in the plan would never run, all for one line of
    evidence. A summary that cannot be kept is logged and left None; the step
    is still delivered, and still disputable. An empty result is None too, so
    a reader has one "nothing to show" value to test for, not two.
    """
    try:
        cleaned = sanitize_untrusted(summary, max_chars=OUTPUT_SUMMARY_MAX_CHARS)
    except Exception:
        logger.warning(
            "task %s step %d: output summary could not be cleaned — settled without it",
            task_id,
            step_index,
            exc_info=True,
        )
        return None
    return cleaned or None


def _execute_refusal(step: PlanStep, info: reputation_svc.RepInfo | None) -> str | None:
    """Why `step` must not be dispatched NOW, or None to dispatch it.

    The routing floor and the listing filter are applied when a plan is BUILT
    (`orchestrator_svc`), and a stored plan used to be executed on that verdict
    alone — so an agent its operator delisted between decompose and execute
    still received the step and the payment (ADR 0006:162, "routing honours
    delisting everywhere a candidate is chosen"). Executing a step IS choosing
    its agent, so the registry is asked again here, at the last moment before
    dispatch, exactly as the decompose clamp asks it before a step is stored.

    The returned sentence is buyer-facing (trace lines are world-readable when
    TASK_AUTH_REQUIRED is off): it names the agent and the reason, never the
    operator's own data.

    `info` is the agent's reputation as read at the START of this run, and the
    routing floor is re-applied to it by three rules:

      * A read that FAILED (`degraded`, the prior served in its place) proves
        nothing about the agent, so it cannot overturn the verdict the buyer
        authorised: the step runs on its plan-time stamps. Refusing here would
        strip a plan the buyer already signed for because the chain was slow —
        and on a warm host the batch read degrades routinely, so that would be
        most plans.
      * A read SUPERSEDED by a rating that landed since (`superseded`: the
        last on-chain value, served while the fresh read is still out) proves
        nothing about the agent's score now either, so it is treated exactly
        like a failed read. Judging it would refuse with a pre-rating bound
        that can sit above the floor — a false "fell below" to the buyer.
      * A read that succeeded and clears the floor dispatches.
      * A read that succeeded and does NOT clear it refuses — the agent is now
        provably below the floor — with one exception: a step the starvation
        backstop re-admitted below the floor at plan time (`step.degraded`).
        The buyer authorised that step knowing it was below the floor, flagged
        inline and with a `floor_relaxed` notice, so it still runs as long as
        its bound is no worse than the one the card showed. Worse than that,
        it is refused like any other: the buyer consented to the evidence
        they saw, not to whatever arrives after.
    """
    agent = state.agents.get(step.agent_id)
    if agent is None:
        # The registry dropped it — registry sync evicts an on-chain record it
        # no longer believes (a reprice past the bounds). The decompose clamp
        # treats a missing agent as unroutable, and so does this.
        return f"{step.agent_id} is no longer in the agent registry"
    if not _is_listed(agent):
        return f"{step.agent_id} was delisted by its operator after this plan was built"
    if info is None or info.degraded or info.superseded or reputation_svc.passes_floor(info):
        return None
    floor = settings.reputation_floor_bps
    if step.degraded:
        shown = step.rep_lower_bound_bps
        if shown is not None and info.lower_bound_bps >= shown:
            return None
        return (
            f"{step.agent_id} fell further below the routing floor than this plan showed "
            f"({info.lower_bound_bps} < {shown if shown is not None else floor} bps)"
        )
    return (
        f"{step.agent_id} fell below the routing floor after this plan was built ({info.lower_bound_bps} < {floor} bps)"
    )


async def execute_plan(
    plan: StoredPlan,
    *,
    auth_id_hex: str | None = None,
    payer: str | None = None,
) -> str:
    """Kicks off execution in the background. Returns the new task_id.

    If `auth_id_hex` + `payer` are provided, the backend performs a real
    on-chain `charge` + `seal` at the end of the run and stores the tx
    hashes on the Task.

    Raises CapacityExhaustedError — before any task is minted — when
    `settings.orchestrator_max_concurrent` workflows are already running,
    so an unbounded burst of executes can't fan out unbounded LLM calls.

    Raises PlanExpiredError — also before any task is minted — when the plan is
    older than `settings.plan_ttl_seconds`. Checked first: a stale plan is refused for
    what it is, whatever the load.

    Raises AuthorizationRefusedError, before any task is minted, when the escrow
    is v2 and the authorization cannot pay for this run: not the caller's,
    already spent, smaller than the plan, or expiring before a worst-case run
    of it could settle (`worst_case_run_seconds`). Against v1 nothing is read.
    """
    if plan_expired(plan):
        logger.warning(
            "execute refused for plan %s: built %.0fs ago, past the %.0fs plan TTL",
            plan.id,
            _wall_clock() - plan.created_at,
            settings.plan_ttl_seconds,
        )
        raise PlanExpiredError(plan.id)
    # Against a v2 escrow, the authorization must be able to pay for this run
    # (ADR 0010). Read before the capacity check, not after, so nothing awaits
    # between counting the slots and taking one.
    authorized_max = await _authorize_for_execute(plan, auth_id_hex, payer) if auth_id_hex and payer else None
    active = sum(1 for t in _background_tasks if not t.done())
    if active >= settings.orchestrator_max_concurrent:
        raise CapacityExhaustedError(f"{active} workflows in flight (limit {settings.orchestrator_max_concurrent})")

    task_id = f"tsk_{secrets.token_hex(8)}"
    # Capability token for reading this task (status/artifact/trace). Lives
    # in state.task_tokens — never on the Task response model; the durable
    # store keeps only its digest — and is only enforced when
    # settings.task_auth_required is on.
    read_token = secrets.token_urlsafe(24)
    task = Task(
        id=task_id,
        intent=plan.intent,
        agents=len(plan.plan.steps),
        spent=0.0,
        status="running",
        # started_at defaults to now; `started` is derived from it per response.
    )
    state.add_task(task, read_token=read_token)

    _track_background_task(
        asyncio.create_task(_run(plan, task_id, auth_id_hex=auth_id_hex, payer=payer, authorized_max=authorized_max))
    )
    # Nothing awaits between starting the run and returning: the router claims
    # the authorization for this task only once this returns, and a wait here
    # that was cancelled would unclaim an authorization the run is already
    # spending. The receipt's write-through wait is the router's, after that.
    return task_id


async def _run(
    plan: StoredPlan,
    task_id: str,
    *,
    auth_id_hex: str | None = None,
    payer: str | None = None,
    authorized_max: int | None = None,
) -> None:
    start = time.monotonic()
    spent = 0.0
    succeeded = 0  # steps that returned output; drives the terminal status
    last_artifact: dict | None = None
    charge_tx: str | None = None
    proof_tx: str | None = None
    onchain = bool(auth_id_hex and payer)
    # Set the moment a settle (or a v2 release) is handed to the chain. A run
    # that ends WITHOUT one — cancelled, killed by shutdown, or failed by a bug
    # — releases the buyer's v2 custody on its way out instead of stranding it
    # until expiry (S4). One that ends after it never does: that settle may
    # still land, and a second one is a replay at best and a race at worst.
    settle_attempted = False

    # Accumulate prior step outputs so later steps can build on them.
    # The kit (if any) is seeded into context up-front so EVERY worker
    # in the pipeline can short-circuit deterministically.
    kit = detect_kit(plan.intent)
    context: dict[str, Any] = {
        "kit": kit.model_dump() if kit is not None else None,
        "intent": plan.intent,
    }
    # What each step actually delivered, by PLAN-STEP INDEX.
    # Separate from `context` because the two are keyed for different readers:
    # `context` is worker-facing and keyed by worker name (a worker asks for
    # context["code.gen"]), while the settler grades a plan STEP.
    # By index for the same reason its two siblings below are, and it was the
    # one of the three that did not get it: keyed by agent_id, one agent hired
    # for two steps overwrote its own first output with its second, and BOTH
    # steps were then rated on whichever one happened to land last. Indexing
    # also retires the agent_name fallback `_submit_ratings` carried, which
    # existed only because worker name and agent_id coincide for a local worker
    # and do NOT for a bound external one ("external.<agent_id>").
    delivered: dict[int, Any] = {}
    # Plan-step INDEXES that produced output — the same steps that incremented
    # `succeeded` and `spent`. Kept by index rather than by agent_id because a
    # plan may use the same agent twice: keyed by agent, a step that failed
    # would be settled as delivered on the strength of a LATER step that
    # succeeded, and story 4.02 would then accept a dispute over work nobody
    # was ever paid for.
    delivered_steps: set[int] = set()
    # What each delivered step produced, as its settlement keeps it (story
    # 4.05) — by plan-step index for the same reason as `delivered_steps`: one
    # agent on two steps produced two different things, and keyed by agent the
    # second would overwrite the first on the step the buyer disputes.
    output_summaries: dict[int, str | None] = {}
    # Plan-step INDEXES that never reached a worker at all — see the resolve
    # branch below. Distinct from "delivered nothing": these are not rated.
    # By index, like the three maps above and for the last of the same reason:
    # resolution fails OPEN and is negative-cached, so one blip can leave an
    # agent unresolved at step 0 and resolvable at step 3 — and keyed by agent
    # that dropped the rating for step 3, which DID deliver and WAS charged for.
    undispatched: set[int] = set()
    # Agent ids that ran on one of OUR workers. The rating scale trusts a
    # first-party response to have delivered something real; an untrusted one
    # has to prove it (ADR 0005 D3). Carried separately because `delivered`
    # holds the output, not its provenance.
    first_party_ids: set[str] = set()

    try:
        await _emit(task_id, start, "input", f"intent received → '{plan.intent}'")
        if kit is not None:
            await _emit(
                task_id,
                start,
                "exec",
                f"kit detected: {kit.kit_id} → {kit.brand.name} ({len(kit.features)} features locked)",
            )
        await _emit(
            task_id,
            start,
            "exec",
            f"orchestrator: decompose → [{', '.join(s.agent_id for s in plan.plan.steps)}]",
        )
        if auth_id_hex and payer:  # equivalent to `onchain`, spelled out to narrow the optionals
            await _emit(
                task_id,
                start,
                "exec",
                f"x402 authorized on-chain by {payer[:4]}…{payer[-4:]} (auth {auth_id_hex[:8]}…)",
            )

        # The routing floor, re-applied at execute (see `_execute_refusal`): one
        # bounded batch read for every agent the plan names, taken now rather
        # than trusted from the stamps on the plan. One read for the run, not
        # one per step — `fetch_reps` caps it at the configured batch deadline,
        # so the worst case delays the first step by that bound once, and the
        # buyer's /execute has already been answered. Listing, which costs
        # nothing to read, is still checked per step at the moment of dispatch.
        agent_ids = sorted({s.agent_id for s in plan.plan.steps})
        fresh = await reputation_svc.fetch_reps(agent_ids) if agent_ids else {}
        unread = [a for a in agent_ids if (i := fresh.get(a)) is None or i.degraded or i.superseded]
        if unread:
            # Said out loud, because it is the one case where a step runs on
            # evidence older than this run: the buyer should know which.
            await _emit(
                task_id,
                start,
                "exec",
                f"reputation re-check unavailable for [{', '.join(unread)}] — "
                "those steps run on the scores this plan was authorised with",
            )

        for step_index, step in enumerate(plan.plan.steps):
            refusal = _execute_refusal(step, fresh.get(step.agent_id))
            if refusal is not None:
                # Story 2.03's rule for a step that fails, applied to a step
                # that is refused: it is skipped, nothing is added to `spent`
                # (so neither the simulated total nor the on-chain charge
                # includes it), the settlement records it as not delivered,
                # and the run carries on with the steps that remain. It is
                # also NOT rated and NOT counted as a failure — the agent was
                # never asked, and a withdrawal is its operator's decision,
                # not a delivery it failed (ADR 0005 D5). By index, like every
                # other per-step set here.
                undispatched.add(step_index)
                logger.warning("task %s step %d: refused at execute — %s", task_id, step_index, refusal)
                await _emit(task_id, start, "error", f"step refused: {refusal} — not dispatched, not charged")
                continue

            # Resolution deliberately stays OUTSIDE the per-step try/except
            # below. It is a lookup, not the step's work: resolve_worker fails
            # OPEN — an unreadable binding store logs and returns None — so the
            # only thing left that could raise here is a bug in resolution
            # itself, which would repeat on every step anyway. Letting that
            # reach the run-level handler (status "failed", stream closed) is
            # therefore the honest outcome, and is what the suite pins.
            worker = await resolve_worker(step.agent_id)
            if worker is None:
                # No local worker and no resolvable binding. A bound agent
                # that could not be resolved degrades identically to one nobody
                # ever registered — the step is skipped, unbilled, and the run
                # continues. Trace lines only reach the SSE viewer and are
                # dropped with the task; every step failure also goes to the
                # server log so an outage is diagnosable after the fact.
                # Never dispatched, so never rated. resolve_worker fails OPEN,
                # which means this branch is also where OUR outage lands — an
                # unreadable binding store returns None exactly like a missing
                # binding, and the read failure is negative-cached, so one blip
                # can hit several steps. Rating here would write a permanent
                # on-chain 20/100 against an operator who was never asked to
                # deliver. "Did not deliver" and "was never asked" are
                # different facts and only the first is theirs (ADR 0005 D5).
                # THIS step, not this agent: the same agent may be resolvable
                # at another step of the plan, and that step's delivery is its
                # own evidence.
                undispatched.add(step_index)
                logger.error("task %s step %s: unknown agent — step skipped", task_id, step.agent_id)
                await _emit(task_id, start, "error", f"unknown agent: {step.agent_id}")
                continue

            await _emit(
                task_id,
                start,
                "exec",
                f"match agent: {worker.name} ({step.agent_id}) — {step.rationale}",
            )

            try:
                output = await asyncio.wait_for(
                    worker.run(plan.intent, step.rationale, context=context),
                    timeout=STEP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "task %s step %s (%s): timed out after %.0fs",
                    task_id,
                    step.agent_id,
                    worker.name,
                    STEP_TIMEOUT_SECONDS,
                )
                # A step that never answered failed as surely as one that
                # raised, and the counter's question — is this agent broken, or
                # was that one bad run — does not care which. Today the
                # external worker's own deadline fires first, so this handler
                # is reached by a LOCAL worker hanging, which was the one
                # failure the streak could not see.
                failure_tracker.record_failure(step.agent_id, STEP_TIMEOUT_FAILURE)
                await _emit(task_id, start, "error", f"{worker.name} timed out")
                continue
            except Exception as e:
                # exc_info: a revoked key / exhausted quota surfaces as a
                # provider exception several frames down — the traceback is the
                # only way to tell those apart without a debugger.
                logger.error(
                    "task %s step %s (%s): failed: %s",
                    task_id,
                    step.agent_id,
                    worker.name,
                    e,
                    exc_info=True,
                )
                # Trace lines are world-readable when TASK_AUTH_REQUIRED is
                # off — the raw exception text stays in the server log above.
                # The CLASS is safe to surface and is the whole point: twelve
                # distinct failures used to render as this one line, so an
                # operator could not tell "you are down" from "you are slow"
                # from "your body is the wrong shape" — three different fixes.
                #
                # Read duck-typed, not by importing a worker's module: the run
                # loop stays worker-agnostic, an unclassified exception falls
                # back to a generic token rather than crashing the classifier,
                # and a future worker classifies itself for free. Same shape as
                # pdax.errors.orizon_code's default (ADR 0005).
                rule = _failure_class(e)
                failure_tracker.record_failure(step.agent_id, rule)
                await _emit(task_id, start, "error", f"{worker.name} failed ({rule})")
                continue

            if not isinstance(output, dict):
                # A worker must hand back a dict; anything else is that STEP's
                # failure — swallowed like a raised exception, not billed, and
                # never allowed to reach _summarize and sink the whole run.
                logger.error(
                    "task %s step %s (%s): returned %s instead of dict — step treated as failed",
                    task_id,
                    step.agent_id,
                    worker.name,
                    type(output).__name__,
                )
                # Counted like any other step failure. The external contract
                # makes this unreachable for a bound operator — parse_operator_output
                # returns a dict or raises — so what lands here is a local
                # worker returning the wrong thing, which is exactly the kind
                # of persistent breakage the streak exists to name.
                failure_tracker.record_failure(step.agent_id, NOT_A_DICT_FAILURE)
                await _emit(task_id, start, "error", f"{worker.name} returned an unusable result")
                continue

            unusable = _unusable_field(output)
            if unusable is not None:
                # Same rule as the non-dict case above, one level in: a field
                # the post-step handling cannot consume is that STEP's failure
                # — unbilled, skipped, the run carries on — rather than an
                # exception escaping to the run-level handler and taking the
                # settlement (and every honest agent's payment) down with it.
                # The field PATH is the diagnostic here — "artifact.files"
                # names the offending value where the top-level type would
                # only say "dict" — and it is a shape, not content, so the
                # operator's own text stays out of the log.
                logger.error(
                    "task %s step %s (%s): output field %r has an unusable shape — step treated as failed",
                    task_id,
                    step.agent_id,
                    worker.name,
                    unusable,
                )
                # One token for every unusable field, not one per path: the
                # field name is the diagnostic and it is already in the log
                # above, while the tracker coalesces on CLASS CHANGE — so
                # spelling the path into the class would let a worker mangling
                # a different field each time flip the guard back into a flood.
                failure_tracker.record_failure(step.agent_id, UNUSABLE_OUTPUT_FAILURE)
                await _emit(task_id, start, "error", f"{worker.name} returned an unusable {unusable}")
                continue

            succeeded += 1
            spent += step.est_price_usdc
            delivered_steps.add(step_index)
            # Clears the streak and emits one recovery INFO, so an endpoint that
            # comes back is as visible in Render as one that broke.
            failure_tracker.record_success(step.agent_id)
            if not onchain:
                await _emit(
                    task_id,
                    start,
                    "cost",
                    f"x402 payment → {step.agent_id} :: {step.est_price_usdc:.3f} USDC (simulated)",
                )
            summary = _summarize(output)
            await _emit(task_id, start, "out", f"{worker.name}: {summary}")
            # Kept from the SAME value the line above traced, not re-derived at
            # settlement, so the dispute form and the trace cannot disagree
            # about what this step produced. Cannot raise — see the helper.
            output_summaries[step_index] = _stored_summary(task_id, step_index, summary)

            # Surface critic notes / violations if the worker reports them.
            if isinstance(output, dict):
                violations = output.get("critic_violations") or []
                if violations:
                    joined = " · ".join(violations)[:180]
                    await _emit(task_id, start, "exec", f"{worker.name}: violations → {joined}")
                notes = output.get("critic_notes") or []
                if notes:
                    joined = " · ".join(notes)[:180]
                    await _emit(task_id, start, "exec", f"{worker.name}: {joined}")
                # Some workers (deploy.v0) attach a synthetic preview URL —
                # surface it so the demo viewer sees the "ship" moment. An
                # unusable or non-http(s) one is dropped, not traced.
                preview_url = _trace_url(output.get("preview_url"))
                if preview_url:
                    await _emit(
                        task_id,
                        start,
                        "out",
                        f"{worker.name}: preview → {preview_url}",
                    )

            # Capture artifact if the worker returned one. Later steps may
            # overwrite this (e.g. code.critic refines code.gen's draft).
            art = output.get("artifact") if isinstance(output, dict) else None
            if art:
                last_artifact = art
                # Operator-chosen text: coerced and capped like every other
                # traced value, so a title cannot flood the buyer's trace.
                title = str(art.get("title", "artifact"))[:180]
                files = art.get("files", [])
                total_bytes = sum(len(f.get("content", "")) for f in files)
                total_lines = sum(f.get("content", "").count("\n") + 1 for f in files)
                await _emit(
                    task_id,
                    start,
                    "artifact",
                    f"▣ {title} — {len(files)} file(s) · {total_lines:,} lines · {total_bytes:,} bytes",
                )

            # Persist this step's output under the agent name so later workers
            # can read it. e.g. context["code.gen"] = {...}.
            if isinstance(output, dict):
                first_party = get_worker(step.agent_id) is worker
                if first_party:
                    first_party_ids.add(step.agent_id)
                # Untrusted prose is fenced on the way IN — this assignment is
                # the single boundary every later step reads through, so a
                # worker added tomorrow gets the defence without knowing it
                # needs one (2.02 AC-5 / Product Rule 5).
                context[worker.name] = output if first_party else _fenced_for_context(output)
                # The settler reads a rating-facing view of the same output —
                # an untrusted worker does not get to grade itself. Under THIS
                # step's index: the agent may serve another step of this plan,
                # and that step's output is its own evidence, not this one's.
                delivered[step_index] = _rating_view(output, first_party=first_party)

        total_steps = len(plan.plan.steps)
        status = _terminal_status(total_steps, succeeded, last_artifact)

        if auth_id_hex and payer:  # equivalent to `onchain`, spelled out to narrow the optionals
            if succeeded == 0 and await _escrow_version() >= 2:
                # v2 holds the buyer's funds in custody from `authorize`, so
                # "bill nothing" is not the same as "do nothing": an empty
                # `settle` releases every stroop back to them now, instead of
                # leaving it locked until they reclaim it after expiry. Nothing
                # is recorded or sealed — there is no delivered work.
                settle_attempted = True
                charge_tx, _, _ = await _settle_v2(
                    task_id,
                    start,
                    plan,
                    payer=payer,
                    auth_id_hex=auth_id_hex,
                    delivered_steps=frozenset(),
                    authorized_max=authorized_max,
                )
                await _submit_ratings(
                    task_id,
                    start,
                    plan,
                    delivered,
                    payer=payer,
                    job_id=unsettled_job_id(task_id),
                    undispatched=frozenset(undispatched),
                    first_party_ids=frozenset(first_party_ids),
                )
            elif succeeded == 0:
                # Same rule as the simulated branch below — a workflow that
                # produced nothing has nothing to attest to, and nothing to
                # bill: charging here would consume the payer's escrow
                # authorization for a dust amount (max(total, 0.000001)) and
                # seal an attestation for an empty job.
                logger.info(
                    "task %s: no step produced output — charge/seal skipped, ratings still submitted"
                    " (auth %s, payer %s)",
                    task_id,
                    auth_id_hex,
                    payer,
                )
                await _emit(
                    task_id,
                    start,
                    "exec",
                    "no agent produced output — skipping on-chain charge/seal",
                    settlement="skipped",
                )
                # Ratings are NOT skipped with them (ADR 0005 D2). Charge and
                # seal are correctly withheld, but ratings run the other way:
                # a run where every step failed is precisely the evidence the
                # routing floor needs, and withholding it meant the canonical
                # broken endpoint — down, failing everything — accumulated no
                # negative evidence at all and stayed routable forever.
                await _submit_ratings(
                    task_id,
                    start,
                    plan,
                    delivered,
                    payer=payer,
                    job_id=unsettled_job_id(task_id),
                    undispatched=frozenset(undispatched),
                    first_party_ids=frozenset(first_party_ids),
                )
            else:
                # The settlement is recorded inside this, the moment the charge
                # confirms and before the seal — and so before the ratings
                # below, which are a SEQUENTIAL run of on-chain submits, one per
                # step, each waiting up to ~30s on a status poll. A process that
                # dies partway through any of them (a Render redeploy, an idle
                # spin-down) would otherwise take the buyer's only evidence of
                # what they paid for with it. It touches no chain, and a store
                # that is down cannot fail the run — see `_record_settlement`.
                settle_attempted = True
                charge_tx, proof_tx, job_id = await _settle_and_record(
                    task_id,
                    start,
                    plan,
                    payer=payer,
                    auth_id_hex=auth_id_hex,
                    total_usdc=spent,
                    delivered_steps=frozenset(delivered_steps),
                    output_summaries=output_summaries,
                    authorized_max=authorized_max,
                )
                # Rated whether or not the money moved, exactly as the
                # no-success branch above is (ADR 0005 D2). This used to sit
                # behind `if charge_tx and job_id`, which made every rating a
                # partial run could produce conditional on a settlement that
                # never happens: _settle_onchain returns (None, None, None)
                # when the charge raises and (charge_tx, None, None) when it
                # comes back non-SUCCESS, so a run where one agent delivered
                # and another did not submitted NOTHING. The agent that failed
                # kept its prior and stayed routable, and the operator who did
                # deliver earned no positive evidence either — the exact
                # asymmetry the story exists to remove. Settlement answers
                # "who gets paid"; a rating answers "who delivered", and the
                # second does not depend on the first.
                await _submit_ratings(
                    task_id,
                    start,
                    plan,
                    delivered,
                    payer=payer,
                    # The job id is minted by the charge, so a run that did not
                    # settle has none. Falling back to the task-derived id is
                    # what lets the evidence land anyway, and it is derived
                    # rather than random so the ledger's (agent_id, job_id)
                    # replay guard still counts one run exactly once.
                    job_id=job_id or unsettled_job_id(task_id),
                    undispatched=frozenset(undispatched),
                    first_party_ids=frozenset(first_party_ids),
                )
        elif status == "complete":
            # Only a run that actually delivered gets a (simulated) seal — a
            # workflow that produced nothing has nothing to attest to — and it
            # counts the agents that delivered, never the plan's (D-086).
            sim_hash = "0x" + secrets.token_hex(16)
            await _emit(task_id, start, "proof", f"ERC-8004 attestation: {sim_hash} (simulated)")
            await _emit(
                task_id,
                start,
                "proof",
                f"workflow sealed — {succeeded} agents · {spent:.3f} USDC · {time.monotonic() - start:.2f}s",
            )

        if status != "complete":
            logger.error(
                "task %s finalized as %s: %d/%d steps produced output, artifact=%s",
                task_id,
                status,
                succeeded,
                total_steps,
                last_artifact is not None,
            )
            await _emit(
                task_id,
                start,
                "error",
                f"workflow incomplete — {succeeded}/{total_steps} agents produced output",
            )

        _finalize_task(task_id, status, spent, last_artifact, charge_tx, proof_tx)

    except asyncio.CancelledError:
        # Shutdown or external cancel: leave the task terminal instead of
        # "running" forever, tell the stream, and keep the cancellation
        # propagating. shield: a second cancel must not kill the trace line.
        _finalize_task(task_id, "failed", spent, last_artifact, charge_tx, proof_tx)
        await asyncio.shield(_emit(task_id, start, "error", "workflow cancelled"))
        if auth_id_hex and payer and not settle_attempted:
            await asyncio.shield(_release_on_exit(task_id, start, auth_id_hex, "run_cancelled"))
        raise
    except Exception as e:
        logger.exception("workflow %s failed", task_id)
        _finalize_task(task_id, "failed", spent, last_artifact, charge_tx, proof_tx)
        await _emit(task_id, start, "error", f"workflow failed: {e}")
        if auth_id_hex and payer and not settle_attempted:
            await _release_on_exit(task_id, start, auth_id_hex, "run_failed")
    finally:
        # The SSE terminator must reach subscribers even mid-cancellation:
        # run the drain-delay + close shielded so bus.close ALWAYS executes
        # (a bare `await asyncio.sleep` here would swallow the close when a
        # CancelledError landed on it).
        await asyncio.shield(_finish_stream(task_id))
        # The run's terminal state, durably, before the run is gone — shielded
        # for the same reason: it is the write a receipt reads after a restart.
        await asyncio.shield(task_persistence.flush(task_persistence.RUN_END_FLUSH_SECONDS))


async def _release_on_exit(task_id: str, start: float, auth_id_hex: str, reason: str) -> None:
    """Release a run's v2 custody on a path that ends it without a settle.

    A no-op on v1 (`release_authorization` returns None there). Only a release
    that CONFIRMED is traced, as `released`; every other outcome is already in
    the log, and the task keeps whatever it said before.
    """
    tx = await release_authorization(auth_id_hex, reason=reason)
    if tx:
        await _emit(task_id, start, "cost", f"custody released to the buyer · tx {tx[:10]}…", settlement="released")


def _terminal_status(total_steps: int, succeeded: int, artifact: dict | None) -> TaskStatus:
    """Terminal status for a run that reached the end of its plan.

    Every per-step failure is swallowed (`continue`) so one bad agent can't sink
    the whole workflow — which means reaching the end of the loop says nothing
    about what was produced. The rule:

      • all steps produced output → "complete". A zero-step plan is vacuously
        complete: nothing was asked of the network, so nothing failed.
      • no step produced output → "failed". This is the total-outage case (key
        revoked, quota exhausted, provider down): every step raises, the loop
        continues past all of them, and the caller gets spent=0 and no artifact.
        Calling that "complete" hands out an empty result and inflates
        /api/metrics/overview's avg_completion, which is a rate over exactly the
        two terminal statuses.
      • partial (some produced output, some failed) → judged on the deliverable:
        "complete" when an artifact survived, because the caller still receives
        the thing they asked for, merely degraded (e.g. code.gen drafted and
        code.critic then failed to polish); "failed" when it did not, because a
        half-run workflow with nothing to hand back is not a success.

    Only "complete" and "failed" are used: TaskStatus is a closed union the
    frontend renders as a status badge, so a partial run must land on one side
    of it rather than inventing a third terminal value.
    """
    if succeeded >= total_steps:
        return "complete"
    if succeeded == 0:
        return "failed"
    return "complete" if artifact else "failed"


def _finalize_task(
    task_id: str,
    status: TaskStatus,
    spent: float,
    artifact: dict | None,
    charge_tx: str | None,
    proof_tx: str | None,
) -> None:
    """Terminal status write shared by the complete / failed / cancelled paths."""
    task = state.tasks.get(task_id)
    if task is None:
        return
    state.put_task(
        task.model_copy(
            update={
                "status": status,
                "spent": round(spent, 4),
                "artifact": artifact,
                "charge_tx": charge_tx,
                "proof_tx": proof_tx,
            }
        )
    )


async def _finish_stream(task_id: str) -> None:
    """Give SSE subscribers a beat to drain, then end the stream."""
    await asyncio.sleep(0.05)
    await bus.close(task_id)


async def _settle_onchain(
    task_id: str,
    start: float,
    plan: StoredPlan,
    *,
    payer: str,
    auth_id_hex: str,
    total_usdc: float,
    on_charged: Callable[[str, bytes], Awaitable[None]] | None = None,
) -> tuple[str | None, str | None, bytes | None]:
    """Perform the real PaymentEscrow.charge + AttestationRegistry.seal calls.

    The v1 path, unchanged by ADR 0010: against a v2 escrow `_settle_and_record`
    runs `_settle_v2` instead, and this is never reached.

    `on_charged(charge_tx, job_id)` is awaited the moment the charge CONFIRMS,
    before the seal is submitted. The seal is another ~30s poll, and a
    cancellation during it (main.py's shutdown drain, a Render redeploy)
    propagates out of here without returning the job id — so whatever must
    survive a confirmed charge has to be written by then, not after.

    Returns (charge_tx, proof_tx, job_id). A hash is returned only for a
    transaction that CONFIRMED (S7): a rejected or unconfirmed charge or seal
    returns None in its place, and its hash is kept in the log line and the
    trace, never on the task or the settlement as if it were evidence. job_id
    is None whenever the charge did not CONFIRM — skipped,
    raised, rejected, or submitted and never confirmed. The last of those is
    not a failure: a charge that timed out may still settle on-chain, so a None
    job id means "we do not know that the money moved", never "it did not".
    The unconfirmed case is logged loudly, because nothing downstream can tell
    it apart from the others — see the branch that raises it.

    Every failure here is money-affecting (a charge that never landed, or a
    charge that landed with no attestation sealed against it), so each one is
    logged as well as traced: trace lines live in state.traces, which is evicted
    after 200 tasks and lost on every restart. The log carries the task, auth,
    payer and amount — never the signing key or raw signed XDR.
    """
    from stellar_sdk import scval as _sv

    from ..stellar import client as sc

    charge_tx: str | None = None
    proof_tx: str | None = None
    settled_job_id: bytes | None = None
    # The id the charge is submitted under, for the reconstruction lines below:
    # a settlement is keyed by it, so a line without it cannot be reconciled.
    job_hex = "-"

    if not settings.stellar_signing_key:
        logger.error(
            "task %s: STELLAR_SIGNING_KEY not set — skipping on-chain charge/seal "
            "(auth %s, payer %s, %.6f USDC unbilled)",
            task_id,
            auth_id_hex,
            payer,
            total_usdc,
        )
        await _emit(
            task_id,
            start,
            "error",
            "STELLAR_SIGNING_KEY not set — skipping on-chain charge/seal",
            settlement="failed",
        )
        return (None, None, None)

    # Same ceiling /api/stellar/server/charge enforces — this path reaches the
    # identical PaymentEscrow.charge, so it must refuse (never clamp) an
    # over-cap total before any money moves.
    if total_usdc > settings.max_charge_usdc:
        logger.error(
            "task %s: total %.6f USDC exceeds MAX_CHARGE_USDC=%.6f — skipping on-chain "
            "charge/seal (auth %s, payer %s, %.6f USDC unbilled)",
            task_id,
            total_usdc,
            settings.max_charge_usdc,
            auth_id_hex,
            payer,
            total_usdc,
        )
        await _emit(
            task_id,
            start,
            "error",
            f"charge {total_usdc:.3f} USDC exceeds cap {settings.max_charge_usdc:.3f} — skipping on-chain charge/seal",
            settlement="failed",
        )
        return (None, None, None)

    try:
        settler = sc._signer_keypair().public_key
        auth_id = bytes.fromhex(auth_id_hex)
        job_id = secrets.token_bytes(16)
        job_hex = job_id.hex()

        total_i128 = sc.usdc_to_i128(max(total_usdc, 0.000001))

        # 1. charge
        charge = await sc.invoke_with_server_key_async(
            sc.contract_ids().payment_escrow,
            "charge",
            [
                sc.addr(settler),
                sc.bytes16(auth_id),
                sc.i128(total_i128),
                sc.bytes16(job_id),
            ],
        )
        charge_tx = charge.get("hash")
        charge_status = str(charge.get("status") or "")
        if charge_status == "SUCCESS" and charge_tx:
            settled_job_id = job_id
            await _emit(
                task_id,
                start,
                "cost",
                f"x402 charge → {total_usdc:.3f} USDC settled · tx {charge_tx[:10]}…",
                settlement="settled",
            )
            if on_charged is not None:
                await on_charged(charge_tx, job_id)
        elif charge_status == "FAILED":
            # The ledger rejected it after simulation passed: nothing moved.
            logger.error(
                "task %s: PaymentEscrow.charge did not settle — status=%s hash=%s "
                "(auth %s, payer %s, %.6f USDC, job %s)",
                task_id,
                charge_status,
                charge_tx,
                auth_id_hex,
                payer,
                total_usdc,
                job_id.hex(),
            )
            await _emit(
                task_id,
                start,
                "error",
                f"charge status={charge_status} hash={charge_tx}",
                settlement="failed",
            )
            return (None, None, None)
        else:
            # NOT a failure: `"timeout"` is the client's word for submitted and
            # then lost track of, and a SUCCESS with no hash is the same
            # unknown. The charge may settle two ledgers from now, and the
            # epic says so everywhere else — `RefundStatus.TIMEOUT` and the
            # CancelledError handler below both spell out that the transfer may
            # still land. It is called out separately here because the
            # consequence is one-sided: we return no job id, `_record_settlement`
            # writes nothing, and if the charge DOES land the buyer is charged
            # for a run `issue_dispute_challenge` answers `unknown_job` for,
            # until the 24-hour window closes on a door that never opened.
            # Recording the settlement anyway would open a dispute window over
            # money that may never have moved, which is a different wrong — so
            # this lane leaves the operator a line they can reconcile from and
            # refund by hand, and the choice between the two is a story of its
            # own.
            logger.error(
                "task %s: PaymentEscrow.charge is UNCONFIRMED and MAY STILL SETTLE — no settlement was "
                "recorded, so if it does the buyer is charged with NO WAY TO DISPUTE it: status=%s hash=%s "
                "(auth %s, payer %s, %.6f USDC, job %s)",
                task_id,
                charge_status or "missing",
                charge_tx,
                auth_id_hex,
                payer,
                total_usdc,
                job_id.hex(),
            )
            # The buyer is told too, in the terms that matter to them: their
            # money may be gone and this run has no dispute window. The job id
            # stays out — trace lines are world-readable when TASK_AUTH_REQUIRED
            # is off, and a dispute is filed against that id.
            await _emit(
                task_id,
                start,
                "error",
                f"charge unconfirmed status={charge_status or 'missing'} hash={charge_tx} — it may still "
                "settle, and this run cannot be disputed",
                settlement="unconfirmed",
            )
            return (None, None, None)

        # 2. seal
        intent_hash = hashlib.sha256(plan.intent.encode("utf-8")).digest()
        agents_sym = _sv.to_vec([sc.sym(s.agent_id) for s in plan.plan.steps])
        # charge() returns the on-chain receipt id (BytesN<16>); include it in
        # the attestation when the tx meta decoded cleanly, else seal without.
        # Coupled to client._finalize_invoke: the decoded return value lands
        # under "result" ("return_value" is _finalize_submit's key, on the
        # user-signed path), and bytes have already been converted with
        # .hex() — so the receipt id arrives as a 32-char hex string.
        receipt_rv = charge.get("result")
        receipt_id: bytes | None = None
        if isinstance(receipt_rv, str):
            try:
                receipt_id = bytes.fromhex(receipt_rv)
            except ValueError:
                receipt_id = None
        elif isinstance(receipt_rv, (bytes, bytearray)):
            receipt_id = bytes(receipt_rv)
        receipts = []
        if receipt_id is not None and len(receipt_id) == 16:
            receipts.append(sc.bytes16(receipt_id))
        else:
            # Sealing without the receipt severs the attestation's only
            # on-chain link to the payment that funded it — never do that
            # silently.
            logger.error(
                "task %s: charge result did not decode to a 16-byte receipt id — "
                "sealing without receipt link (result=%r, job %s, charge_tx %s, auth %s, payer %s)",
                task_id,
                receipt_rv,
                job_id.hex(),
                charge_tx,
                auth_id_hex,
                payer,
            )
            await _emit(
                task_id,
                start,
                "error",
                "charge receipt id missing — sealing attestation without receipt link",
            )
        receipts_vec = _sv.to_vec(receipts)

        seal = await sc.invoke_with_server_key_async(
            sc.contract_ids().attestation_registry,
            "seal",
            [
                sc.addr(settler),  # caller / sealer
                sc.bytes16(job_id),
                sc.addr(payer),  # orchestrator = the payer for now
                sc.bytes32(intent_hash),
                agents_sym,
                receipts_vec,
                sc.i128(total_i128),
            ],
        )
        proof_tx = seal.get("hash")
        if seal.get("status") == "SUCCESS" and proof_tx:
            await _emit(
                task_id,
                start,
                "proof",
                f"ERC-8004 attestation sealed · tx {proof_tx[:10]}…",
            )
            await _emit(
                task_id,
                start,
                "proof",
                f"workflow sealed — {len(plan.plan.steps)} agents · {total_usdc:.3f} USDC · "
                f"{time.monotonic() - start:.2f}s",
            )
        else:
            # The charge already settled, so this leaves paid work unattested —
            # the one state that has to be reconstructable from the logs.
            logger.error(
                "task %s: AttestationRegistry.seal did not settle — status=%s hash=%s "
                "(job %s charged %.6f USDC via tx %s, auth %s, payer %s)",
                task_id,
                seal.get("status"),
                proof_tx,
                job_id.hex(),
                total_usdc,
                charge_tx,
                auth_id_hex,
                payer,
            )
            await _emit(
                task_id,
                start,
                "error",
                f"seal status={seal.get('status')} hash={proof_tx}",
            )
            # Never handed on as the proof: a seal that did not confirm proves
            # nothing, and the task and the settlement would show it as if it
            # did (S7). The hash stays in the log line and the trace above.
            proof_tx = None
    except asyncio.CancelledError:
        # CancelledError is a BaseException, so the handler below never sees
        # it — yet a shutdown cancel (main.py's drain window) can land between
        # the charge submit and its confirmation, when the charge may still
        # settle on-chain. The reconstruction log must fire before the
        # cancellation propagates; cancellation semantics are preserved by
        # re-raising.
        logger.error(
            "task %s: on-chain settlement cancelled mid-flight "
            "(job %s, auth %s, payer %s, %.6f USDC, charge_tx=%s, proof_tx=%s)",
            task_id,
            job_hex,
            auth_id_hex,
            payer,
            total_usdc,
            charge_tx,
            proof_tx,
        )
        raise
    except Exception as e:
        logger.error(
            "task %s: on-chain settlement failed: %s (job %s, auth %s, payer %s, %.6f USDC, charge_tx=%s, proof_tx=%s)",
            task_id,
            e,
            job_hex,
            auth_id_hex,
            payer,
            total_usdc,
            charge_tx,
            proof_tx,
            exc_info=True,
        )
        if settled_job_id is not None:
            # The charge confirmed and something after it failed — the seal,
            # or the settlement write. The money moved; the line is as it was.
            await _emit(task_id, start, "error", "on-chain settlement failed")
        elif isinstance(e, sc.InFlightError):
            # Sent, then lost: the charge may still land, exactly as the
            # unconfirmed branch above says of a poll that timed out.
            await _emit(
                task_id,
                start,
                "error",
                "on-chain settlement unconfirmed — it may still settle, and this run cannot be disputed",
                settlement="unconfirmed",
            )
        else:
            await _emit(task_id, start, "error", "on-chain settlement failed", settlement="failed")

    return (charge_tx, proof_tx, settled_job_id)


# ── PaymentEscrow v2: custody at authorize, one settle that pays each step ──
# ADR 0010. v1's `charge` could never move the payer's funds (D-039); v2 holds
# them from `authorize` and pays each delivered step's operator its own amount
# in one `settle`, returning the rest. `_settle_onchain` above stays the v1
# path, byte for byte, and `_settle_and_record` picks between the two by the
# escrow's own `version()`.

# The run-time budget an authorization must still have left at `/execute`.
# Everything below is a ceiling this module or the client already enforces,
# not a measurement: the re-check's batch read (`reputation_batch_timeout_seconds`,
# once), then per step the outer deadline (`STEP_TIMEOUT_SECONDS`) plus the
# lookups and trace writes around it, then the settle itself. The settle's
# allowance is the submit profile in `client._server` — load_account, prepare
# and send, each up to 15 s with one retry — plus the transaction's own 30 s
# validity window for it to close in a ledger, with slack for the owner reads
# before it. v2 does not refuse a settle for being past `expires_at`, but from
# that moment the payer may `reclaim`, and whichever lands first wins — so a
# run that outlives its authorization is work that may never be paid for.
STEP_OVERHEAD_SECONDS = 5.0
SETTLE_ALLOWANCE_SECONDS = 150.0

# How long an on-chain owner read is trusted. A registered agent's owner is
# written at registration and the registry has no transfer entrypoint, so a
# positive answer is cached long; "not registered" can end the moment the
# operator registers, so it is cached briefly. A failed read is not cached.
OWNER_READ_TTL_SECONDS = 900.0
NO_OWNER_READ_TTL_SECONDS = 60.0
_owner_reads: dict[str, tuple[str | None, float]] = {}

# Why a delivered step was not paid, as the settlement records it.
UNPAID_FREE = "free"
UNPAID_NO_OWNER = "no_onchain_owner"
UNPAID_OWNER_UNREADABLE = "owner_unreadable"
UNPAID_OVER_CAP = "over_authorized_cap"


def worst_case_run_seconds(step_count: int) -> float:
    """The longest a paid run of `step_count` steps can take before its settle lands."""
    return (
        settings.reputation_batch_timeout_seconds
        + step_count * (STEP_TIMEOUT_SECONDS + STEP_OVERHEAD_SECONDS)
        + SETTLE_ALLOWANCE_SECONDS
    )


async def _escrow_version() -> int:
    """The configured escrow's version: 2 settles, 1 charges.

    Cached per contract id by the client once it has a definite answer. An
    UNREADABLE version is read as 1, and is not cached, so the next run asks
    again. That is the safe reading on both deployments: against v1 it is
    today's path exactly, and against v2 the v1 `charge` does not exist, so
    its simulation is refused before anything is signed or sent — the run is
    marked `failed`, no money moves, and the buyer's custody stays reclaimable
    after `expires_at`. It never lets a v2 settle go out unchecked, because
    the v2 checks (`_authorize_for_execute`, the cap re-check) only ever run on
    a definite 2.
    """
    from ..stellar import client as sc

    escrow = sc.contract_ids().payment_escrow
    if not escrow:
        return 1
    cached = sc.cached_escrow_version(escrow)
    if cached is not None:
        return cached
    try:
        return await asyncio.to_thread(sc.escrow_version, escrow)
    except Exception as e:
        logger.warning("PaymentEscrow %s version unreadable — settling as v1 this time: %s", escrow, e)
        return 1


# The contract errors a settle of someone's authorization can be refused with
# that mean "not ours to settle any more": no such authorization, the payer
# reclaimed it, or it was already settled. None of them moved money.
_NOT_SETTLED_BY_US = {2: "not_found", 6: "reclaimed", 7: "already_settled"}


async def _submit_release(auth_id_hex: str, job_id: bytes) -> tuple[SettlementState, str | None, str]:
    """Submit an empty v2 `settle`: the whole custody back to the payer.

    Returns (state, tx_hash, detail). Never raises. `state` is `released` only
    when the transaction CONFIRMED; `unconfirmed` whenever it may still land;
    `failed` when it definitely moved nothing. The caller has already checked
    the escrow is v2 and a signing key is set.
    """
    from ..stellar import client as sc

    try:
        result = await sc.invoke_with_server_key_async(
            sc.contract_ids().payment_escrow,
            "settle",
            [
                sc.addr(sc._signer_keypair().public_key),
                sc.bytes16(bytes.fromhex(auth_id_hex)),
                sc.bytes16(job_id),
                sc.payouts_vec([]),
            ],
        )
    except sc.ContractError as e:
        return "failed", None, f"refused: {_NOT_SETTLED_BY_US.get(e.code, f'contract error #{e.code}')}"
    except sc.InFlightError as e:
        return "unconfirmed", e.tx_hash, f"in flight: {e}"
    except Exception as e:
        return "failed", None, f"not submitted: {e}"
    tx = result.get("hash")
    status = str(result.get("status") or "")
    if status == "SUCCESS" and tx:
        return "released", tx, "confirmed"
    if status == "FAILED":
        return "failed", None, f"ledger rejected tx {tx}"
    return "unconfirmed", tx, f"status={status or 'missing'} tx {tx}"


async def release_authorization(auth_id_hex: str, *, reason: str) -> str | None:
    """Return a v2 authorization's whole custody to its payer. Never raises.

    FROZEN SIGNATURE — other lanes call this (the execute and stellar routers
    among them), so it changes only by agreement.

    Submits `settle(settler, auth_id, <fresh job id>, [])`, which pays nobody
    and returns every stroop of `max_amount` to the payer, for the paths that
    end a paid run without a settle: nothing delivered, a run cancelled or
    killed by shutdown, a settle refused before it was submitted, an execute
    refused after the buyer had already authorized.

    `reason` is a short token naming that path; it goes into the log line
    only, never on-chain and never into a trace.

    Returns the transaction hash when the release CONFIRMED, and None in every
    other case: a v1 escrow (a no-op — v1 holds no custody), no signing key,
    an escrow version that cannot be read, a release refused (already settled,
    already reclaimed, no such authorization), rejected, or unconfirmed. Every
    outcome is logged. An UNCONFIRMED release may still land and is never
    retried, for `_settle_onchain`'s reason: a second settle of the same
    authorization is a replay at best and a race at worst.
    """
    try:
        if await _escrow_version() < 2:
            logger.info(
                "release of authorization %s (%s) skipped: escrow is v1, nothing is in custody", auth_id_hex, reason
            )
            return None
        if not settings.stellar_signing_key:
            logger.error(
                "release of authorization %s (%s) NOT submitted: STELLAR_SIGNING_KEY not set — the payer can "
                "reclaim it after expiry",
                auth_id_hex,
                reason,
            )
            return None
        if len(bytes.fromhex(auth_id_hex)) != 16:
            raise ValueError("an authorization id is 16 bytes")
        outcome, tx, detail = await _submit_release(auth_id_hex, secrets.token_bytes(16))
    except Exception as e:
        logger.error("release of authorization %s (%s) NOT submitted: %s", auth_id_hex, reason, e, exc_info=True)
        return None
    if outcome == "released":
        logger.info("released authorization %s to its payer (%s): tx %s", auth_id_hex, reason, tx)
        return tx
    if outcome == "unconfirmed":
        logger.error(
            "release of authorization %s (%s) is UNCONFIRMED and MAY STILL LAND — never retried: %s",
            auth_id_hex,
            reason,
            detail,
        )
    else:
        logger.error("release of authorization %s (%s) did not happen: %s", auth_id_hex, reason, detail)
    return None


class AuthorizationRefusedError(CodedHTTPException):
    """`/execute` refused a v2 authorization before anything ran.

    Raised only against a v2 escrow, where the authorization is custody the
    settle must be able to spend: one that is not the caller's, not for this
    plan, already spent, too small for the plan, or too close to expiry to
    outlive a worst-case run would buy work nobody can be paid for. Before any
    task is minted, like `PlanExpiredError`, and an HTTP exception for the same
    reason. Messages say what to do, never a configured limit.
    """


_REAUTHORIZE = "authorize this plan again and execute with the new authorization"


async def _authorize_for_execute(plan: StoredPlan, auth_id_hex: str, payer: str) -> int | None:
    """Check a v2 authorization can pay for `plan`; its max in stroops, or None on v1.

    v1 is left exactly as it was — nothing is read and nothing refused — for
    the reason `_escrow_version` gives: its charge cannot settle anyway, so a
    refusal there would only cost the live demo its runs.
    """
    from ..stellar import client as sc

    if await _escrow_version() < 2:
        return None
    escrow = sc.contract_ids().payment_escrow
    try:
        auth = await asyncio.to_thread(sc.escrow_authorization, escrow, bytes.fromhex(auth_id_hex))
    except Exception as e:
        logger.warning("execute refused: authorization %s unreadable on %s: %s", auth_id_hex, escrow, e)
        raise AuthorizationRefusedError(
            503, "authorization_unreadable", "the authorization could not be read on-chain — try again shortly"
        ) from e
    if auth is None:
        raise AuthorizationRefusedError(404, "authorization_not_found", f"no such authorization — {_REAUTHORIZE}")
    if auth.payer != payer:
        # The payer is who a dispute credit is paid to, so a run must never
        # settle one wallet's custody under another wallet's name.
        logger.warning("execute refused: authorization %s belongs to %s, not %s", auth_id_hex, auth.payer, payer)
        raise AuthorizationRefusedError(
            403, "authorization_payer_mismatch", "this authorization was made by a different wallet"
        )
    if auth.agent_id != plan.id:
        # The interface binds an authorization's label to the plan it pays for
        # (finding S2), so one plan's custody cannot pay for another plan.
        logger.warning("execute refused: authorization %s is for %s, not plan %s", auth_id_hex, auth.agent_id, plan.id)
        raise AuthorizationRefusedError(
            409, "authorization_plan_mismatch", f"this authorization was made for a different plan — {_REAUTHORIZE}"
        )
    if auth.settled or auth.revoked:
        raise AuthorizationRefusedError(
            409, "authorization_spent", f"this authorization is already spent — {_REAUTHORIZE}"
        )
    try:
        plan_total = sum(_stroops(step.est_price_usdc) for step in plan.plan.steps)
    except _PayoutRefused:
        plan_total = auth.max_amount + 1  # a price the ledger cannot hold is one no authorization covers
    if plan_total > auth.max_amount:
        raise AuthorizationRefusedError(
            409, "authorization_insufficient", f"this authorization does not cover the plan — {_REAUTHORIZE}"
        )
    remaining = auth.expires_at - _wall_clock()
    needed = worst_case_run_seconds(len(plan.plan.steps))
    if remaining < needed:
        logger.warning(
            "execute refused: authorization %s has %.0fs left, a %d-step run can need %.0fs",
            auth_id_hex,
            remaining,
            len(plan.plan.steps),
            needed,
        )
        raise AuthorizationRefusedError(
            409,
            "authorization_expiring",
            f"this authorization expires before a run of this plan could be paid for — {_REAUTHORIZE}",
        )
    return auth.max_amount


def _stroops(amount_usdc: float) -> int:
    """`amount_usdc` in stroops, refusing what the ledger cannot hold."""
    from ..stellar import client as sc

    if not math.isfinite(amount_usdc) or amount_usdc < 0:
        raise _PayoutRefused(f"price {amount_usdc!r} is not a finite, non-negative amount")
    return sc.usdc_to_i128(amount_usdc)


class _PayoutRefused(Exception):
    """The payouts for a run cannot be built, so no settle is submitted."""


@dataclass(frozen=True)
class _StepPayout:
    """What one delivered step is paid, which `payouts` entry pays it, and why not."""

    step_index: int
    agent_id: str
    amount: int  # stroops; 0 when the step is not paid
    payout_index: int | None  # None when the step is not paid
    unpaid_reason: str | None = None  # one of the UNPAID_* tokens when not paid


@dataclass(frozen=True)
class _PayoutPlan:
    """The `payouts` a settle sends, and how every delivered step maps onto them."""

    payouts: tuple[Any, ...]  # client.Payout, kept Any so this module imports the client lazily
    steps: tuple[_StepPayout, ...]
    clamped_stroops: int = 0  # how much the authorized cap cut; 0 when it cut nothing

    @property
    def total(self) -> int:
        return sum(p.amount for p in self.payouts)


def _payout_plan(
    plan: StoredPlan,
    delivered_steps: frozenset[int],
    unpaid_agents: Mapping[str, str],
    cap: int | None = None,
) -> _PayoutPlan:
    """Build `settle`'s payouts from the steps that DELIVERED, and nothing else.

    `delivered_steps` is the run loop's own set — the steps that produced
    usable output and moved `spent` — so a step that timed out, raised,
    returned the wrong shape or was never dispatched is not in it and is not
    paid (5.01 AC5). Each amount is that step's price in stroops.

    A delivered step is still not paid, and its share goes back to the buyer
    with the remainder, when:
      - it was free: the contract refuses a zero payout;
      - its agent is in `unpaid_agents` — no on-chain owner (the seeded `agt_*`
        catalogue), or an owner that could not be confirmed. `settle` pays
        `owner_of(agent_id)`, and ONE agent the registry does not hold reverts
        the whole transaction, so only confirmed owners are named;
      - `cap`, the authorization's `max_amount`, is already used up. Steps are
        paid in plan order and the one that crosses the cap is cut to what is
        left; `clamped_stroops` says how much was cut, so the caller can say
        so loudly. The contract would refuse the whole settle (`Insufficient`)
        instead, and the planner's rounding once made that reachable (S6).

    At most `MAX_SETTLE_PAYOUTS` entries. One entry per step keeps a receipt
    per step. Past the limit, entries are merged per agent, in first-seen
    order — every step still paid its own amount, sharing its agent's receipt
    — and a run with more distinct agents than that is refused whole rather
    than paid in part. The planner caps a plan at six steps, so neither is
    reachable from a planned run today; they bound what a stored plan could
    carry.
    """
    from ..stellar import client as sc

    remaining = cap
    clamped = 0
    paid: list[tuple[int, str, int]] = []
    unpaid: list[_StepPayout] = []
    for index in sorted(delivered_steps):
        step = plan.plan.steps[index]
        amount = _stroops(step.est_price_usdc)
        reason = UNPAID_FREE if amount <= 0 else unpaid_agents.get(step.agent_id)
        if reason is None and remaining is not None:
            if amount > remaining:
                clamped += amount - remaining
                amount = remaining
            remaining -= amount
            if amount <= 0:
                reason = UNPAID_OVER_CAP
        if reason is not None:
            unpaid.append(_StepPayout(index, step.agent_id, 0, None, reason))
        else:
            paid.append((index, step.agent_id, amount))

    payouts: list[Any] = []
    steps: list[_StepPayout] = list(unpaid)
    if len(paid) <= sc.MAX_SETTLE_PAYOUTS:
        for index, agent_id, amount in paid:
            steps.append(_StepPayout(index, agent_id, amount, len(payouts)))
            payouts.append(sc.Payout(agent_id, amount))
    else:
        slot: dict[str, int] = {}
        totals: list[int] = []
        for index, agent_id, amount in paid:
            if agent_id not in slot:
                slot[agent_id] = len(totals)
                totals.append(0)
            totals[slot[agent_id]] += amount
            steps.append(_StepPayout(index, agent_id, amount, slot[agent_id]))
        if len(totals) > sc.MAX_SETTLE_PAYOUTS:
            raise _PayoutRefused(
                f"{len(totals)} distinct agents delivered, "
                f"more than the {sc.MAX_SETTLE_PAYOUTS} payouts one settle accepts"
            )
        payouts = [sc.Payout(agent_id, totals[i]) for agent_id, i in slot.items()]
    return _PayoutPlan(tuple(payouts), tuple(sorted(steps, key=lambda s: s.step_index)), clamped)


def _onchain_owner_sync(agent_id: str) -> str | None:
    """`AgentRegistry.owner_of(agent_id)`: the owner, or None when the registry
    answers NotFound (#2). Raises on anything else. Cached per agent id —
    long for an owner, briefly for "none" — and a failure is never cached."""
    from ..stellar import client as sc

    hit = _owner_reads.get(agent_id)
    if hit is not None and hit[1] > time.monotonic():
        return hit[0]
    registry = settings.stellar_agent_registry
    if not registry:
        raise RuntimeError("AgentRegistry is not configured (STELLAR_AGENT_REGISTRY is unset)")
    try:
        owner = sc.simulate_read(registry, "owner_of", [sc.sym(agent_id)], load_source=False)
    except RuntimeError as e:
        if sc._contract_error_code(str(e).removeprefix("simulate failed: ")) != 2:
            raise
        owner = None
    if owner is not None and not (isinstance(owner, str) and owner):
        raise RuntimeError(f"owner_of returned {type(owner).__name__}, not an address")
    ttl = OWNER_READ_TTL_SECONDS if owner else NO_OWNER_READ_TTL_SECONDS
    _owner_reads[agent_id] = (owner, time.monotonic() + ttl)
    return owner


async def _unpaid_agents(agent_ids: set[str]) -> dict[str, str]:
    """The agents among `agent_ids` a settle must NOT name, and why.

    Only a confirmed on-chain owner is paid (interface amendment, S1): one
    payout naming an agent the registry does not hold reverts the whole settle,
    every other operator's pay with it. An owner that could not be READ is
    left out on the same ground — its step is recorded unpaid, with its own
    reason, and its share goes back to the buyer.
    """

    async def _check(agent_id: str) -> tuple[str, str | None]:
        try:
            owner = await asyncio.to_thread(_onchain_owner_sync, agent_id)
        except Exception as e:
            logger.error("owner of %s unreadable before settle — its steps are not paid: %s", agent_id, e)
            return agent_id, UNPAID_OWNER_UNREADABLE
        return agent_id, None if owner else UNPAID_NO_OWNER

    results = await asyncio.gather(*(_check(a) for a in sorted(agent_ids)))
    return {agent_id: reason for agent_id, reason in results if reason is not None}


async def _settle_refused(
    task_id: str, start: float, auth_id_hex: str, payer: str, reason: str, trace: str
) -> tuple[str | None, str | None, bytes | None]:
    """A settle refused before it was built: say so, then release the custody.

    Nothing was sent, so the buyer's funds would otherwise sit in custody until
    they reclaim them after expiry (S4). The release is best-effort and never
    retried; its own outcome is logged by `release_authorization`.
    """
    logger.error(
        "task %s: PaymentEscrow.settle NOT submitted — %s (auth %s, payer %s); releasing the custody",
        task_id,
        reason,
        auth_id_hex,
        payer,
    )
    await _emit(task_id, start, "error", f"settlement refused before submitting — {trace}", settlement="failed")
    await release_authorization(auth_id_hex, reason="settle_refused")
    return (None, None, None)


async def _settle_v2(
    task_id: str,
    start: float,
    plan: StoredPlan,
    *,
    payer: str,
    auth_id_hex: str,
    delivered_steps: frozenset[int],
    authorized_max: int | None = None,
    on_settled: Callable[[str, bytes, _PayoutPlan, list[bytes] | None], Awaitable[None]] | None = None,
) -> tuple[str | None, str | None, bytes | None]:
    """PaymentEscrow v2 `settle`, then the attestation seal.

    The v2 twin of `_settle_onchain`, and deliberately the same contract with
    its caller: returns (settle_tx, proof_tx, job_id), and `on_settled` is
    awaited the moment the settle CONFIRMS, before the seal, for the reason
    `_settle_onchain` gives. A hash is returned only for a transaction that
    CONFIRMED (S7); a rejected or unconfirmed one is in the log and the trace.
    The outcome lands on the task and its trace as `settlement` (ADR 0010).

    With no delivered steps it is a release: an empty `payouts` returns the
    buyer's whole custody and pays nobody, and there is nothing to record or
    seal. The job id is then `unsettled_job_id`, the one the run's ratings are
    written under anyway.

    Before anything is signed the payouts are built from the delivered steps,
    held under the authorization's own `max_amount` (read back from the chain
    unless `/execute` already read it) and checked against `MAX_CHARGE_USDC`.
    A settle refused there releases the custody instead.

    The authorization's expiry is NOT checked: the amended interface lets a
    settle land after `expires_at` until the payer reclaims, and a settle that
    loses that race comes back `Revoked` — or `Replay` if something else
    settled it — which is reported as not settled by us.

    An UNCONFIRMED settle is handled exactly as an unconfirmed charge: it may
    still land, so it is reported `unconfirmed`, no settlement is recorded, and
    it is NEVER retried — a second settle of one authorization is refused as a
    replay at best, and at worst would race the first.
    """
    from stellar_sdk import scval as _sv

    from ..stellar import client as sc

    settle_tx: str | None = None
    proof_tx: str | None = None
    settled_job_id: bytes | None = None
    job_hex = "-"
    total = 0

    if not settings.stellar_signing_key:
        logger.error(
            "task %s: STELLAR_SIGNING_KEY not set — skipping on-chain settle/seal "
            "(auth %s, payer %s, %d steps delivered)",
            task_id,
            auth_id_hex,
            payer,
            len(delivered_steps),
        )
        await _emit(
            task_id, start, "error", "STELLAR_SIGNING_KEY not set — skipping on-chain settle/seal", settlement="failed"
        )
        return (None, None, None)

    escrow = sc.contract_ids().payment_escrow
    if authorized_max is None and delivered_steps:
        try:
            auth = await asyncio.to_thread(sc.escrow_authorization, escrow, bytes.fromhex(auth_id_hex))
        except Exception as e:
            return await _settle_refused(
                task_id, start, auth_id_hex, payer, f"authorization unreadable: {e}", "the authorization is unreadable"
            )
        if auth is None:
            return await _settle_refused(
                task_id, start, auth_id_hex, payer, "authorization not found", "the authorization was not found"
            )
        if auth.payer != payer or auth.agent_id != plan.id:
            # `/execute` skipped its check (an escrow version it could not read
            # at the time), so this is the first look at whose custody this is.
            # Not this payer's, or not for this plan: never settle it, and never
            # release it either — it is somebody else's money to reclaim.
            logger.error(
                "task %s: PaymentEscrow.settle NOT submitted — authorization %s is %s's for %s, not %s's for %s",
                task_id,
                auth_id_hex,
                auth.payer,
                auth.agent_id,
                payer,
                plan.id,
            )
            await _emit(
                task_id,
                start,
                "error",
                "settlement refused — the authorization is not this buyer's for this plan",
                settlement="failed",
            )
            return (None, None, None)
        authorized_max = auth.max_amount
    try:
        unpaid_agents = (
            await _unpaid_agents({plan.plan.steps[i].agent_id for i in delivered_steps}) if delivered_steps else {}
        )
        payout_plan = _payout_plan(plan, delivered_steps, unpaid_agents, authorized_max)
    except _PayoutRefused as e:
        return await _settle_refused(task_id, start, auth_id_hex, payer, str(e), "the payouts could not be built")
    total = payout_plan.total
    if payout_plan.clamped_stroops:
        logger.error(
            "task %s: payouts CLAMPED by %d stroops to the authorized max %s — the plan priced more than the "
            "buyer authorized (auth %s, payer %s)",
            task_id,
            payout_plan.clamped_stroops,
            authorized_max,
            auth_id_hex,
            payer,
        )
        await _emit(task_id, start, "error", "payouts cut to what the buyer authorized")
    for unpaid in (s for s in payout_plan.steps if s.unpaid_reason in (UNPAID_NO_OWNER, UNPAID_OWNER_UNREADABLE)):
        logger.error(
            "task %s: step %d (%s) delivered but is NOT paid (%s) — its share returns to the buyer (auth %s)",
            task_id,
            unpaid.step_index,
            unpaid.agent_id,
            unpaid.unpaid_reason,
            auth_id_hex,
        )
        await _emit(
            task_id, start, "error", f"{unpaid.agent_id} has no confirmed on-chain owner — its step is not paid"
        )

    if total > sc.usdc_to_i128(settings.max_charge_usdc):
        return await _settle_refused(
            task_id,
            start,
            auth_id_hex,
            payer,
            f"payouts total {total} stroops exceed MAX_CHARGE_USDC={settings.max_charge_usdc}",
            "the payouts exceed the charge cap",
        )

    release = not payout_plan.payouts
    total_usdc = total / reputation_svc.STROOPS_PER_USDC
    receipts: list[bytes] | None = None
    try:
        settler = sc._signer_keypair().public_key
        auth_id = bytes.fromhex(auth_id_hex)
        job_id = secrets.token_bytes(16) if delivered_steps else unsettled_job_id(task_id)
        job_hex = job_id.hex()

        settle = await sc.invoke_with_server_key_async(
            escrow,
            "settle",
            [
                sc.addr(settler),
                sc.bytes16(auth_id),
                sc.bytes16(job_id),
                sc.payouts_vec(list(payout_plan.payouts)),
            ],
        )
        tx = settle.get("hash")
        settle_status = str(settle.get("status") or "")
        if settle_status == "SUCCESS" and tx:
            settle_tx = tx
            settled_job_id = job_id
            receipts = sc.receipt_ids(settle.get("result"))
            if receipts is not None and len(receipts) != len(payout_plan.payouts):
                receipts = None
            if release:
                await _emit(
                    task_id,
                    start,
                    "cost",
                    f"x402 settle → nothing paid, custody released to the buyer · tx {tx[:10]}…",
                    settlement="released",
                )
            else:
                await _emit(
                    task_id,
                    start,
                    "cost",
                    f"x402 settle → {total_usdc:.3f} USDC paid to {len(payout_plan.payouts)} "
                    f"operator payout(s), the rest released · tx {tx[:10]}…",
                    settlement="settled",
                )
            if on_settled is not None and delivered_steps:
                await on_settled(tx, job_id, payout_plan, receipts)
        elif settle_status == "FAILED":
            logger.error(
                "task %s: PaymentEscrow.settle did not settle — status=%s hash=%s "
                "(auth %s, payer %s, %d stroops, job %s)",
                task_id,
                settle_status,
                tx,
                auth_id_hex,
                payer,
                total,
                job_hex,
            )
            await _emit(task_id, start, "error", f"settle status={settle_status} hash={tx}", settlement="failed")
            if not release:
                # The ledger rejected it: definitive, nothing moved, so the
                # custody would sit until the payer reclaims it (S4).
                await release_authorization(auth_id_hex, reason="settle_rejected")
            return (None, None, None)
        else:
            # `_settle_onchain`'s unconfirmed branch, for the same reasons: it
            # may still land, nothing is recorded, and it is never retried.
            logger.error(
                "task %s: PaymentEscrow.settle is UNCONFIRMED and MAY STILL SETTLE — no settlement was "
                "recorded, so if it does the buyer is charged with NO WAY TO DISPUTE it: status=%s hash=%s "
                "(auth %s, payer %s, %d stroops, job %s)",
                task_id,
                settle_status or "missing",
                tx,
                auth_id_hex,
                payer,
                total,
                job_hex,
            )
            await _emit(
                task_id,
                start,
                "error",
                f"settle unconfirmed status={settle_status or 'missing'} hash={tx} — it may still "
                "settle, and this run cannot be disputed",
                settlement="unconfirmed",
            )
            return (None, None, None)

        if not delivered_steps:
            return (settle_tx, None, settled_job_id)
        if release:
            # Delivered, but nobody could be paid (no confirmed on-chain owner,
            # free, or the cap was spent), so the settle moved nothing to any
            # operator. An attestation needs a payment to attest to: one naming
            # no agent and no receipt says nothing, and one naming the unpaid
            # agents would claim payments that never happened (D-086).
            await _emit(task_id, start, "exec", "no agent was paid — nothing to attest, no seal submitted")
            return (settle_tx, None, settled_job_id)

        # Seal, exactly as v1 does, with one receipt per payout rather than one
        # for the run: the attestation's link to every payment that funded it.
        # The agents are the PAID ones, one per payout, so `agents[i]` and
        # `receipts[i]` name the same payment (D-086). Sealing every plan step
        # attested work to an agent that timed out, failed or was refused —
        # and the seal is permanent on-chain evidence a buyer or an indexer
        # reads as "this agent delivered and was paid for this job".
        sealed_agents = [payout.agent_id for payout in payout_plan.payouts]
        if receipts is None and payout_plan.payouts:
            logger.error(
                "task %s: settle result did not decode to %d receipt ids — sealing without receipt links "
                "(result=%r, job %s, settle_tx %s, auth %s, payer %s)",
                task_id,
                len(payout_plan.payouts),
                settle.get("result"),
                job_hex,
                settle_tx,
                auth_id_hex,
                payer,
            )
            await _emit(
                task_id, start, "error", "settle receipt ids missing — sealing attestation without receipt links"
            )
        intent_hash = hashlib.sha256(plan.intent.encode("utf-8")).digest()
        seal = await sc.invoke_with_server_key_async(
            sc.contract_ids().attestation_registry,
            "seal",
            [
                sc.addr(settler),
                sc.bytes16(job_id),
                sc.addr(payer),
                sc.bytes32(intent_hash),
                _sv.to_vec([sc.sym(agent_id) for agent_id in sealed_agents]),
                _sv.to_vec([sc.bytes16(r) for r in receipts or []]),
                sc.i128(total),
            ],
        )
        seal_tx = seal.get("hash")
        if seal.get("status") == "SUCCESS" and seal_tx:
            proof_tx = seal_tx
            await _emit(task_id, start, "proof", f"ERC-8004 attestation sealed · tx {seal_tx[:10]}…")
            await _emit(
                task_id,
                start,
                "proof",
                f"workflow sealed — {len(sealed_agents)} agents · {total_usdc:.3f} USDC · "
                f"{time.monotonic() - start:.2f}s",
            )
        else:
            logger.error(
                "task %s: AttestationRegistry.seal did not settle — status=%s hash=%s "
                "(job %s settled %d stroops via tx %s, auth %s, payer %s)",
                task_id,
                seal.get("status"),
                seal_tx,
                job_hex,
                total,
                settle_tx,
                auth_id_hex,
                payer,
            )
            await _emit(task_id, start, "error", f"seal status={seal.get('status')} hash={seal_tx}")
    except asyncio.CancelledError:
        logger.error(
            "task %s: on-chain settle cancelled mid-flight (job %s, auth %s, payer %s, %d stroops, "
            "settle_tx=%s, proof_tx=%s)",
            task_id,
            job_hex,
            auth_id_hex,
            payer,
            total,
            settle_tx,
            proof_tx,
        )
        raise
    except Exception as e:
        logger.error(
            "task %s: on-chain settle failed: %s (job %s, auth %s, payer %s, %d stroops, settle_tx=%s, proof_tx=%s)",
            task_id,
            e,
            job_hex,
            auth_id_hex,
            payer,
            total,
            settle_tx,
            proof_tx,
            exc_info=True,
        )
        if settled_job_id is not None:
            await _emit(task_id, start, "error", "on-chain settlement failed")
        elif isinstance(e, sc.InFlightError):
            await _emit(
                task_id,
                start,
                "error",
                "on-chain settlement unconfirmed — it may still settle, and this run cannot be disputed",
                settlement="unconfirmed",
            )
        elif isinstance(e, sc.ContractError) and e.code in _NOT_SETTLED_BY_US:
            # Reclaimed by the payer first, already settled, or gone: nothing
            # this settle did moved money, and there is nothing to release.
            await _emit(
                task_id,
                start,
                "error",
                f"not settled by us — the authorization was {_NOT_SETTLED_BY_US[e.code].replace('_', ' ')}",
                settlement="failed",
            )
        else:
            await _emit(task_id, start, "error", "on-chain settlement failed", settlement="failed")
            if isinstance(e, sc.NotSubmittedError) and not release:
                # Refused before it was sent — by the simulation, the signer or
                # the RPC — so nothing is in flight, and the custody would sit
                # until the payer reclaims it. Hand it back instead (S4). Not
                # for a release: that IS the call that was just refused.
                await release_authorization(auth_id_hex, reason="settle_not_submitted")

    return (settle_tx, proof_tx, settled_job_id)


def _settled_usdc(total_usdc: float) -> float:
    """What the charge actually moved, back in USDC.

    `_settle_onchain` charges `usdc_to_i128(max(total_usdc, 0.000001))`: the
    run's estimate floored to dust and rounded to the ledger's 7 decimals. So
    the sum of the plan's estimates is NOT what the buyer paid, and a dispute
    credit computed from that sum could hand back more than ever came out of
    escrow. Derived through the charge's own helper rather than restated, so
    the two cannot drift apart.
    """
    from ..stellar import client as sc
    from .reputation_svc import STROOPS_PER_USDC

    return sc.usdc_to_i128(max(total_usdc, 0.000001)) / STROOPS_PER_USDC


def _v2_settlement_record(
    task_id: str,
    plan: StoredPlan,
    *,
    payer: str,
    auth_id_hex: str,
    job_id: bytes,
    charge_tx: str | None,
    proof_tx: str | None,
    delivered_steps: frozenset[int],
    output_summaries: Mapping[int, str | None],
    payout_plan: _PayoutPlan,
    receipts: list[bytes] | None,
    settled_at: float,
    window_closes_at: float,
) -> SettlementRecord:
    """The settlement a v2 `settle` leaves: each step as it was actually paid.

    A delivered step's `price_usdc` is what its payout moved (see
    `SettlementStep.paid_usdc`), so a credit for it can never exceed it. An
    undelivered step keeps the plan's quote for display, is paid 0.0, and
    stays undisputable as before. `receipts` is in `payouts` order; a step
    shares its payout's receipt, and has none when the ids did not decode.
    """
    paid = {s.step_index: s for s in payout_plan.steps}
    stroops = reputation_svc.STROOPS_PER_USDC

    def _step(index: int, step: PlanStep) -> SettlementStep:
        payout = paid.get(index) if index in delivered_steps else None
        if payout is None:
            return SettlementStep(
                step_index=index,
                agent_id=step.agent_id,
                agent_name=step.agent_name,
                price_usdc=step.est_price_usdc,
                delivered=False,
                output_summary=None,
                paid_usdc=0.0,
            )
        amount = payout.amount / stroops
        receipt = (
            receipts[payout.payout_index].hex() if receipts is not None and payout.payout_index is not None else None
        )
        return SettlementStep(
            step_index=index,
            agent_id=step.agent_id,
            agent_name=step.agent_name,
            price_usdc=amount,
            delivered=True,
            output_summary=output_summaries.get(index),
            paid_usdc=amount,
            receipt_id_hex=receipt,
            unpaid_reason=payout.unpaid_reason,
        )

    return SettlementRecord(
        task_id=task_id,
        payer=payer,
        auth_id_hex=auth_id_hex,
        job_id_hex=job_id.hex(),
        charge_tx=charge_tx,
        proof_tx=proof_tx,
        settled_usdc=payout_plan.total / stroops,
        steps=tuple(_step(index, step) for index, step in enumerate(plan.plan.steps)),
        settled_at=settled_at,
        window_closes_at=window_closes_at,
    )


async def _record_settlement(
    task_id: str,
    start: float,
    plan: StoredPlan,
    *,
    payer: str,
    auth_id_hex: str,
    job_id: bytes | None,
    charge_tx: str | None,
    proof_tx: str | None,
    total_usdc: float,
    delivered_steps: frozenset[int],
    output_summaries: Mapping[int, str | None],
    payout_plan: _PayoutPlan | None = None,
    receipts: list[bytes] | None = None,
) -> SettlementRecord | None:
    """Write the one record a dispute is later judged against (story 4.02).

    `payout_plan` is given for a v2 settle (ADR 0010), and then the record
    keeps what each step was ACTUALLY paid: `paid_usdc`, the receipt its
    payout minted, and — because v2 charges per step — the same amount as the
    step's `price_usdc`, the number every credit is computed from. A step
    that delivered and was not paid (free, or an agent with no on-chain owner)
    is recorded at 0.0 and so cannot be credited. `settled_usdc` is the sum
    of the payouts, never the plan's estimate.

    Nothing else keeps these facts. The job id is minted inside the charge and
    dies with `_settle_onchain`'s frame, the payer is a parameter of `_run`,
    and `state.tasks` holds no per-step price, no link back to the plan and no
    settlement time — it evicts finished tasks first and is lost on restart,
    which is exactly the set and exactly the moment a buyer disputes.

    Written only when the charge CONFIRMED. `job_id` comes back None when the
    charge was skipped (no signing key, over the cap), raised, was rejected, or
    was submitted and never confirmed — and the last of those is NOT a charge
    that took nothing. A timed-out charge may still settle, exactly as
    `refund_svc.RefundStatus` says of a timed-out transfer, and this function
    cannot tell the two apart from a None. So a run with no record here is a
    run we cannot prove was paid for, not a run we know was free: when an
    unconfirmed charge does land, the buyer is charged and has no window, which
    `_settle_onchain` logs as the unreconciled charge it is.

    It is written as soon as the charge confirms, with `proof_tx` None, and
    before the seal is even submitted (`_settle_and_record`): the buyer paid,
    so the buyer has recourse, attested or not — and whether or not the
    process lives through the seal. The seal's hash follows in a second row.

    Returns the record it wrote, or None when it wrote nothing.

    Best-effort in the same sense as `_submit_ratings`, and for a stronger
    reason: the money has already moved by the time this runs, so a store that
    is down must not fail or stall the workflow on top of it. It is logged at
    ERROR — not warning — because the buyer silently loses every route to a
    refund, and the trace line that says so is evicted long before they notice.
    """
    if job_id is None:
        return None

    settled_at = time.time()
    # Stamped, never recomputed on read: the buyer is told a closing time in
    # the trace below, and tuning DISPUTE_WINDOW_SECONDS afterwards must not
    # move the deadline for work that is already paid for.
    window_closes_at = settled_at + settings.dispute_window_seconds

    try:
        if payout_plan is not None:
            record = _v2_settlement_record(
                task_id,
                plan,
                payer=payer,
                auth_id_hex=auth_id_hex,
                job_id=job_id,
                charge_tx=charge_tx,
                proof_tx=proof_tx,
                delivered_steps=delivered_steps,
                output_summaries=output_summaries,
                payout_plan=payout_plan,
                receipts=receipts,
                settled_at=settled_at,
                window_closes_at=window_closes_at,
            )
        else:
            record = SettlementRecord(
                task_id=task_id,
                payer=payer,
                auth_id_hex=auth_id_hex,
                job_id_hex=job_id.hex(),
                charge_tx=charge_tx,
                proof_tx=proof_tx,
                settled_usdc=_settled_usdc(total_usdc),
                steps=tuple(
                    SettlementStep(
                        step_index=index,
                        agent_id=step.agent_id,
                        agent_name=step.agent_name,
                        # The plan's estimate: the charge moves one total for the
                        # run, never a price per step. `settled_usdc` below is what
                        # it moved, and every credit is bounded by that.
                        price_usdc=step.est_price_usdc,
                        # A step that failed, or that no worker ever resolved
                        # for, delivered nothing and was never billed — 4.02
                        # refuses to dispute it. Same condition that moved
                        # `succeeded` and `spent` in the run loop.
                        delivered=index in delivered_steps,
                        # Already cleaned and bounded by `_stored_summary` in
                        # the run loop. Gated on delivery here as well, so "an
                        # undelivered step has no summary" holds where the
                        # record is built rather than only where it was fed.
                        output_summary=output_summaries.get(index) if index in delivered_steps else None,
                    )
                    for index, step in enumerate(plan.plan.steps)
                ),
                settled_at=settled_at,
                window_closes_at=window_closes_at,
            )
        await get_dispute_store().record_settlement(record)
    except Exception as e:
        logger.error(
            "task %s: settlement NOT recorded: %s — the buyer has no way to dispute this run "
            "(job %s, charge_tx %s, proof_tx %s, auth %s, payer %s, %.6f USDC)",
            task_id,
            e,
            job_id.hex(),
            charge_tx,
            proof_tx,
            auth_id_hex,
            payer,
            total_usdc,
            exc_info=True,
        )
        # The buyer is told too: a window they cannot actually use must not
        # appear in their trace as if it were open.
        await _emit(task_id, start, "error", "settlement not recorded — this run cannot be disputed")
        return None

    if record.settled_usdc <= 0:
        # A v2 settle that paid nobody (every delivered step free or unowned):
        # every step is refused as `nothing_was_charged`, so there is no window
        # to promise — announcing one would send the buyer to a door that
        # cannot open.
        return record
    # The window is a promise, so it is made in the buyer's own record of the
    # run. The job id stays OUT of it: trace lines are world-readable when
    # TASK_AUTH_REQUIRED is off, and the dispute is filed against that id.
    await _emit(
        task_id,
        start,
        "cost",
        "dispute window open — any delivered step can be disputed until "
        f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(window_closes_at))}",
    )
    return record


async def _record_proof(task_id: str, record: SettlementRecord, proof_tx: str) -> None:
    """Add the seal's hash to a settlement already recorded at charge time.

    The settlement tables are append-only and the newest row for a job wins,
    so this APPENDS the same record again with `proof_tx` filled in — every
    other field, `settled_at` and `window_closes_at` above all, is the first
    row's own, so the buyer's deadline cannot move. Best-effort for
    `_record_settlement`'s reason, and a failure here costs less: the window
    is already open, and only the link to the attestation is missing.
    """
    try:
        await get_dispute_store().record_settlement(replace(record, proof_tx=proof_tx))
    except Exception as e:
        logger.error(
            "task %s: the seal's proof tx was NOT added to the settlement: %s — the dispute window is open,"
            " the record lacks its attestation link (job %s, charge_tx %s, proof_tx %s)",
            task_id,
            e,
            record.job_id_hex,
            record.charge_tx,
            proof_tx,
            exc_info=True,
        )


async def _settle_and_record(
    task_id: str,
    start: float,
    plan: StoredPlan,
    *,
    payer: str,
    auth_id_hex: str,
    total_usdc: float,
    delivered_steps: frozenset[int],
    output_summaries: Mapping[int, str | None],
    authorized_max: int | None = None,
) -> tuple[str | None, str | None, bytes | None]:
    """Charge, record the settlement, seal, then record the seal — in that order.

    Against a v2 escrow the charge is `_settle_v2`'s one `settle`, paying each
    delivered step its own amount, and the record keeps those per-step
    payments and receipts; everything below holds for it unchanged. Which
    path runs is the escrow's own `version()` (`_escrow_version`).

    The order is the point. The record used to be written after
    `_settle_onchain` returned, which is after the seal's ~30s poll; a
    cancellation during that poll (a redeploy's shutdown drain) left a buyer
    whose charge CONFIRMED with no settlement and so no dispute window. Now the
    settlement is written the moment the charge confirms, with `proof_tx`
    None, and the seal's hash is appended afterwards when there is one.

    If that first write did not happen — the store failed it, or it was never
    asked — it is attempted once more after the seal, with everything then
    known. Returns what `_settle_onchain` returns.
    """
    if await _escrow_version() >= 2:
        return await _settle_and_record_v2(
            task_id,
            start,
            plan,
            payer=payer,
            auth_id_hex=auth_id_hex,
            delivered_steps=delivered_steps,
            output_summaries=output_summaries,
            authorized_max=authorized_max,
        )

    recorded: list[SettlementRecord] = []

    async def _on_charged(charge_tx: str, job_id: bytes) -> None:
        record = await _record_settlement(
            task_id,
            start,
            plan,
            payer=payer,
            auth_id_hex=auth_id_hex,
            job_id=job_id,
            charge_tx=charge_tx,
            proof_tx=None,
            total_usdc=total_usdc,
            delivered_steps=delivered_steps,
            output_summaries=output_summaries,
        )
        if record is not None:
            recorded.append(record)

    charge_tx, proof_tx, job_id = await _settle_onchain(
        task_id, start, plan, payer=payer, auth_id_hex=auth_id_hex, total_usdc=total_usdc, on_charged=_on_charged
    )
    if recorded:
        if proof_tx is not None:
            await _record_proof(task_id, recorded[0], proof_tx)
    else:
        await _record_settlement(
            task_id,
            start,
            plan,
            payer=payer,
            auth_id_hex=auth_id_hex,
            job_id=job_id,
            charge_tx=charge_tx,
            proof_tx=proof_tx,
            total_usdc=total_usdc,
            delivered_steps=delivered_steps,
            output_summaries=output_summaries,
        )
    return charge_tx, proof_tx, job_id


async def _settle_and_record_v2(
    task_id: str,
    start: float,
    plan: StoredPlan,
    *,
    payer: str,
    auth_id_hex: str,
    delivered_steps: frozenset[int],
    output_summaries: Mapping[int, str | None],
    authorized_max: int | None,
) -> tuple[str | None, str | None, bytes | None]:
    """`_settle_and_record`'s order over `_settle_v2`: settle, record, seal, record the seal."""
    recorded: list[SettlementRecord] = []
    settled: list[tuple[str, bytes, _PayoutPlan, list[bytes] | None]] = []

    async def _on_settled(
        settle_tx: str, job_id: bytes, payout_plan: _PayoutPlan, receipts: list[bytes] | None
    ) -> None:
        settled.append((settle_tx, job_id, payout_plan, receipts))
        record = await _record_settlement(
            task_id,
            start,
            plan,
            payer=payer,
            auth_id_hex=auth_id_hex,
            job_id=job_id,
            charge_tx=settle_tx,
            proof_tx=None,
            # Only the NOT-recorded log line reads it on this path: the record
            # itself is built from the payouts.
            total_usdc=payout_plan.total / reputation_svc.STROOPS_PER_USDC,
            delivered_steps=delivered_steps,
            output_summaries=output_summaries,
            payout_plan=payout_plan,
            receipts=receipts,
        )
        if record is not None:
            recorded.append(record)

    settle_tx, proof_tx, job_id = await _settle_v2(
        task_id,
        start,
        plan,
        payer=payer,
        auth_id_hex=auth_id_hex,
        delivered_steps=delivered_steps,
        authorized_max=authorized_max,
        on_settled=_on_settled,
    )
    if recorded:
        if proof_tx is not None:
            await _record_proof(task_id, recorded[0], proof_tx)
    elif settled:
        tx, settled_job_id, payout_plan, receipts = settled[0]
        await _record_settlement(
            task_id,
            start,
            plan,
            payer=payer,
            auth_id_hex=auth_id_hex,
            job_id=settled_job_id,
            charge_tx=tx,
            proof_tx=proof_tx,
            # Only the NOT-recorded log line reads it on this path: the record
            # itself is built from the payouts.
            total_usdc=payout_plan.total / reputation_svc.STROOPS_PER_USDC,
            delivered_steps=delivered_steps,
            output_summaries=output_summaries,
            payout_plan=payout_plan,
            receipts=receipts,
        )
    return settle_tx, proof_tx, job_id


# A failure class is a token, never free text. Validated by SHAPE rather than
# membership because the run loop must not import a worker's module to classify
# its exception (ADR 0005) — so anything that is not a plain lowercase token
# collapses to one generic value, the way pdax.errors.orizon_code defaults.
# This guards a WORLD-READABLE surface: without it a hostile `rule` attribute
# would put a URL or a key straight into the buyer's trace.
_FAILURE_CLASS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
UNCLASSIFIED_FAILURE = "unclassified"

# The three step failures that carry no exception to classify: the outer
# deadline, and the two output-shape gates. They are spelled here rather than
# inline because they belong to the same closed vocabulary `_failure_class`
# hands the tracker — one naming, one shape, one place to read them all.
STEP_TIMEOUT_FAILURE = "step_timeout"
NOT_A_DICT_FAILURE = "not_a_dict"
UNUSABLE_OUTPUT_FAILURE = "unusable_output"


def _failure_class(exc: BaseException) -> str:
    """The operator-facing class of `exc`, or `unclassified`."""
    rule = getattr(exc, "rule", None)
    return rule if isinstance(rule, str) and _FAILURE_CLASS_RE.match(rule) else UNCLASSIFIED_FAILURE


def unsettled_job_id(task_id: str) -> bytes:
    """The rating id for a run that settled no money.

    `_settle_onchain` mints a random job id as part of the charge, so a run
    that never charges has none — which is why ratings used to be skipped
    outright when nothing succeeded. That is exactly backwards: a run where
    every step failed is the case the routing floor most needs evidence from.

    Derived rather than random for the same reason `dispute_rating.dispute_job_id`
    is: `ReputationLedger.submit` guards replay on `(agent_id, job_id)`, so a
    deterministic id means re-running the same task cannot double-count the
    same failure, while staying linkable to the run that produced it.
    """
    return hashlib.sha256(task_id.encode("utf-8") + b"unsettled").digest()[:16]


# Domain separation for the settler's per-step rating ids, versioned for the
# reason every other `orizon-*:v1` string in this service is, and for one more:
# the ledger's replay guard remembers every key it has ever seen, so a later
# scheme ships under a NEW tag rather than re-deriving ids already spent.
SETTLEMENT_ID_TAG = b"orizon-settlement:v1"

# The shape ADR 0009 D1 fixed for the dispute rating — half the sealed job id
# verbatim so a reviewer SEES the link, half a hash that carries the step.
# Restated rather than imported from `dispute_rating`, which pulls the stellar
# client into its module scope; this module imports that per function instead.
# The ledger's job id is a `BytesN<16>`; the step is packed into two of them,
# which bounds a plan at 65,536 steps — far past anything an orchestrator emits.
_JOB_ID_BYTES = 16
_LINKED_PREFIX_BYTES = 8
_STEP_INDEX_BYTES = 2


def settlement_job_id(job_id: bytes, step_index: int) -> bytes:
    """The id the settler's automatic rating for one plan step is written under.

    Step 0 keeps the sealed job id itself. Every later step takes
    `job_id[:8] ‖ sha256(job_id ‖ SETTLEMENT_ID_TAG ‖ step)[:8]`.

    **Why derive at all.** `ReputationLedger.submit` guards on
    `Rated(agent_id, job_id)` and answers `Error::Replay` *before* it reads
    `kind`, so a plan that hires one agent for two steps submitted both its
    ratings under one pair: the first landed, the second was refused, and half
    that run's evidence was lost behind a "reputation submit failed" line. It is
    the same R12 collision story 4.04 removed from the *dispute* path (ADR 0009
    D1); the settlement path never got the fix.

    **Why step 0 is not derived.** The sealed job id appears verbatim on the
    charge, on the attestation and on every automatic rating this settler has
    ever written. Leaving step 0 on it means no id already on the ledger changes
    meaning — only the later steps, which is exactly the set that could never be
    rated before, move.

    **Why this shape rather than a second scheme.** ADR 0009 D1 chose it so that
    a reviewer who opens a rating on Stellar Expert can tie it to the sealed job
    without insider knowledge, which is what SOW §6.1 asks for: the first
    sixteen hex characters of a derived id ARE the job's own, read by eye off
    the charge or the seal. The hash half is the confirmation anyone can
    recompute — the step index is the position in the plan, whose ordered agent
    list the attestation itself seals. The tag differs from
    `dispute_rating.DISPUTE_ID_TAG`, so a step's automatic rating and its
    dispute rating can never derive onto one key.

    Raises `ValueError` rather than returning an unusable id — a job id that is
    not the ledger's sixteen bytes, a step that will not fit two, or the one
    derivation in 2**64 that reproduces the job id itself and would be refused
    as a replay of step 0's rating for ever. The caller rates the other steps.
    """
    if len(job_id) != _JOB_ID_BYTES:
        raise ValueError(f"a sealed job id is {_JOB_ID_BYTES} bytes, got {len(job_id)}")
    if not 0 <= step_index < 2 ** (8 * _STEP_INDEX_BYTES):
        raise ValueError(f"step index {step_index} does not fit the derived id")
    if step_index == 0:
        return job_id
    digest = hashlib.sha256(job_id + SETTLEMENT_ID_TAG + step_index.to_bytes(_STEP_INDEX_BYTES, "big")).digest()
    derived = job_id[:_LINKED_PREFIX_BYTES] + digest[:_LINKED_PREFIX_BYTES]
    if derived == job_id:
        raise ValueError(f"the rating id for job {job_id.hex()} step {step_index} equals the job id itself")
    return derived


async def _submit_ratings(
    task_id: str,
    start: float,
    plan: StoredPlan,
    delivered: Mapping[int, Any],
    *,
    payer: str,
    job_id: bytes,
    undispatched: frozenset[int] = frozenset(),
    first_party_ids: frozenset[str] = frozenset(),
) -> None:
    """Submit the settler's synthetic per-step ratings to ReputationLedger.

    `delivered` and `undispatched` are both by PLAN-STEP INDEX, not by agent:
    one step's rating is graded on that step's own output and withheld on that
    step's own dispatch, and a plan is free to hire one agent twice. A step
    with no `delivered` entry delivered nothing and is rated as such.

    Best-effort by design: a failed rating never fails the workflow — each step
    traces and logs its own failure and the loop moves on. It is logged as well
    as traced because a rating that silently never landed skews the on-chain
    reputation the planner reads, and traces do not survive a restart.
    """
    gap = rating_writer.config_gap()
    if gap is not None:
        # This used to be a bare `return`: a deployment missing any of these
        # settings rated nothing and said nothing, which is how the testnet
        # ledger sat at zero ratings with nobody able to say why. The operator
        # now gets a WARNING naming the setting (at most hourly), and the
        # buyer's trace says the ratings were not written and why.
        rating_writer.note_skipped(task_id, gap)
        await _emit(task_id, start, "error", f"ratings not submitted: {gap.reason}")
        return

    from ..stellar import client as sc

    # Sequential on purpose: parallel submits from the one scorer account
    # collide on sequence numbers (each tx consumes the account's next seq).
    for step_index, step in enumerate(plan.plan.steps):
        if step_index in undispatched:
            # We never sent them this step, so there is nothing to judge. By
            # index: the agent may have served another step of this plan, and
            # a delivery of ours they never got asked for does not cancel one
            # they did.
            continue
        # By index, so the step is graded on ITS output. The lookup used to be
        # by agent_id — the one identity both worker kinds share, worker names
        # being no use because a bound external agent runs as
        # "external.<agent_id>" rather than as the operator's catalog name —
        # but an agent hired for two steps of one plan wrote both its outputs
        # to that one key, so both steps were graded on whichever landed last.
        step_output = delivered.get(step_index)
        # Untrusted output must carry something checkable to earn the base
        # score. Without this an operator answering "{\"ok\": true}" forever
        # scored 70 — the prior exactly — and their lower bound ROSE with every
        # such reply, so lying outranked failing honestly (ADR 0005 D3).
        rating, weight = reputation_svc.synthetic_rating(
            step_output, step.est_price_usdc, first_party=step.agent_id in first_party_ids
        )
        # One id per STEP, because the ledger's replay guard is per
        # (agent, job): under the job's own id an agent hired twice landed one
        # rating and lost the other. Derived outside the submit's `try` so a
        # refusal to derive is never traced to the buyer as an RPC failure,
        # which is the only thing `failure_reason` could call it.
        try:
            step_job_id = settlement_job_id(job_id, step_index)
        except ValueError:
            logger.error(
                "task %s: no rating id for %s (%s) at step %d of job %s — that step is NOT rated "
                "(rating %d, weight %d, payer %s)",
                task_id,
                step.agent_name,
                step.agent_id,
                step_index,
                job_id.hex(),
                rating,
                weight,
                payer,
                exc_info=True,
            )
            await _emit(
                task_id,
                start,
                "error",
                f"reputation submit skipped for {step.agent_name}: no rating id for this step",
            )
            continue
        try:
            # Async path: the submit RPC runs in a worker thread but the ~30s
            # status poll waits on the event loop — no executor thread pinned.
            result = await sc.submit_rating_async(
                step.agent_id,
                step_job_id,
                rating,
                weight,
                payer,
                "auto",
            )
            tx = result.get("hash") or ""
            status = result.get("status")
            if status != "SUCCESS":
                # Sent but not landed: the ledger FAILED it after simulation
                # passed, or it was still unconfirmed when the poll budget ran
                # out. Neither wrote a rating, and both used to be traced as
                # "rated N/100" — a success line for evidence that never landed.
                logger.error(
                    "task %s: reputation submit for %s (%s) did not land: status=%s tx=%s "
                    "(job %s, step %d under %s, rating %d, weight %d, payer %s)",
                    task_id,
                    step.agent_name,
                    step.agent_id,
                    status,
                    tx,
                    job_id.hex(),
                    step_index,
                    step_job_id.hex(),
                    rating,
                    weight,
                    payer,
                )
                await _emit(
                    task_id,
                    start,
                    "error",
                    f"reputation submit failed for {step.agent_name}: "
                    f"{rating_writer.unlanded_reason(status)} · tx {tx[:10]}…",
                )
                continue
            # Landed, so the score every reader sees has moved: drop the cached
            # rep_state, as a landed dispute rating already does. Without this
            # a plan decomposed inside the read TTL was routed and stamped on
            # the pre-run score — worst after a failed run's 20/100, the very
            # evidence the floor exists to act on. Only here, past the SUCCESS
            # check: a rating that failed or is still unconfirmed changed
            # nothing on the ledger, and dropping the entry for it would only
            # buy an extra RPC read of the same score.
            reputation_svc.invalidate_rep(step.agent_id)
            await _emit(
                task_id,
                start,
                "proof",
                f"reputation → {step.agent_name} rated {rating}/100 · tx {tx[:10]}…",
            )
        except Exception as e:
            # Both ids: the sealed one ties the line to the run, and the one
            # the rating was submitted under is what an operator searches the
            # ledger with. They are the same id only for step 0.
            logger.error(
                "task %s: reputation submit failed for %s (%s): %s "
                "(job %s, step %d under %s, rating %d, weight %d, payer %s)",
                task_id,
                step.agent_name,
                step.agent_id,
                e,
                job_id.hex(),
                step_index,
                step_job_id.hex(),
                rating,
                weight,
                payer,
                exc_info=True,
            )
            # The reason is a closed vocabulary — the ledger's own error name,
            # or "rpc error" — never `e`'s text: the trace is world-readable
            # and the simulation error behind a rejection runs to the whole
            # diagnostic event log. The log line above keeps the full detail.
            await _emit(
                task_id,
                start,
                "error",
                f"reputation submit failed for {step.agent_name}: {rating_writer.failure_reason(e)}",
            )
