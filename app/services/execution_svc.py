from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from typing import Any

from ..agents.registry import get_worker
from ..agents.workers.prompt_safety import fence_untrusted
from ..config import settings
from ..demo_kits import detect_kit
from ..schemas import StoredPlan, Task, TaskStatus, TraceLevel, TraceLine
from ..state import state
from ..trace_bus import bus
from . import failure_tracker, rating_writer
from .binding_registry import resolve_worker

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


async def _emit(task_id: str, start: float, level: TraceLevel, msg: str) -> TraceLine:
    line = TraceLine(t=_now_ts(start), level=level, msg=msg)
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
    """
    active = sum(1 for t in _background_tasks if not t.done())
    if active >= settings.orchestrator_max_concurrent:
        raise CapacityExhaustedError(f"{active} workflows in flight (limit {settings.orchestrator_max_concurrent})")

    task_id = f"tsk_{secrets.token_hex(8)}"
    # Capability token for reading this task (status/artifact/trace). Lives
    # in state.task_tokens — never on the Task response model — and is only
    # enforced when settings.task_auth_required is on.
    read_token = secrets.token_urlsafe(24)
    task = Task(
        id=task_id,
        intent=plan.intent,
        agents=len(plan.plan.steps),
        spent=0.0,
        status="running",
        # started_at defaults to now; `started` is derived from it per response.
    )
    state.add_task(task)
    state.task_tokens[task_id] = read_token

    _track_background_task(asyncio.create_task(_run(plan, task_id, auth_id_hex=auth_id_hex, payer=payer)))
    return task_id


async def _run(
    plan: StoredPlan,
    task_id: str,
    *,
    auth_id_hex: str | None = None,
    payer: str | None = None,
) -> None:
    start = time.monotonic()
    spent = 0.0
    succeeded = 0  # steps that returned output; drives the terminal status
    last_artifact: dict | None = None
    charge_tx: str | None = None
    proof_tx: str | None = None
    onchain = bool(auth_id_hex and payer)

    # Accumulate prior step outputs so later steps can build on them.
    # The kit (if any) is seeded into context up-front so EVERY worker
    # in the pipeline can short-circuit deterministically.
    kit = detect_kit(plan.intent)
    context: dict[str, Any] = {
        "kit": kit.model_dump() if kit is not None else None,
        "intent": plan.intent,
    }
    # What each step actually delivered, keyed by the PLAN STEP's agent_id.
    # Separate from `context` because the two are keyed for different readers:
    # `context` is worker-facing and keyed by worker name (a worker asks for
    # context["code.gen"]), while the settler needs the output for a step it
    # holds only an agent_id and a catalog agent_name for. Those coincide for a
    # local worker and do NOT for a bound external one, whose worker name is
    # "external.<agent_id>" — see _submit_ratings.
    delivered: dict[str, Any] = {}
    # Plan-step INDEXES that produced output — the same steps that incremented
    # `succeeded` and `spent`. Kept by index rather than by agent_id like
    # `delivered` above because a plan may use the same agent twice: keyed by
    # agent, a step that failed would be settled as delivered on the strength
    # of a LATER step that succeeded, and story 4.02 would then accept a
    # dispute over work nobody was ever paid for.
    delivered_steps: set[int] = set()
    # Agent ids whose step never reached a worker at all — see the resolve
    # branch below. Distinct from "delivered nothing": these are not rated.
    undispatched: set[str] = set()
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

        for step_index, step in enumerate(plan.plan.steps):
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
                undispatched.add(step.agent_id)
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
            await _emit(task_id, start, "out", f"{worker.name}: {_summarize(output)}")

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
                # an untrusted worker does not get to grade itself.
                delivered[step.agent_id] = _rating_view(output, first_party=first_party)

        total_steps = len(plan.plan.steps)
        status = _terminal_status(total_steps, succeeded, last_artifact)

        if auth_id_hex and payer:  # equivalent to `onchain`, spelled out to narrow the optionals
            if succeeded == 0:
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
                charge_tx, proof_tx, job_id = await _settle_onchain(
                    task_id, start, plan, payer=payer, auth_id_hex=auth_id_hex, total_usdc=spent
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
            # workflow that produced nothing has nothing to attest to.
            sim_hash = "0x" + secrets.token_hex(16)
            await _emit(task_id, start, "proof", f"ERC-8004 attestation: {sim_hash} (simulated)")
            await _emit(
                task_id,
                start,
                "proof",
                f"workflow sealed — {total_steps} agents · {spent:.3f} USDC · {time.monotonic() - start:.2f}s",
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
        raise
    except Exception as e:
        logger.exception("workflow %s failed", task_id)
        _finalize_task(task_id, "failed", spent, last_artifact, charge_tx, proof_tx)
        await _emit(task_id, start, "error", f"workflow failed: {e}")
    finally:
        # The SSE terminator must reach subscribers even mid-cancellation:
        # run the drain-delay + close shielded so bus.close ALWAYS executes
        # (a bare `await asyncio.sleep` here would swallow the close when a
        # CancelledError landed on it).
        await asyncio.shield(_finish_stream(task_id))


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
    state.tasks[task_id] = task.model_copy(
        update={
            "status": status,
            "spent": round(spent, 4),
            "artifact": artifact,
            "charge_tx": charge_tx,
            "proof_tx": proof_tx,
        }
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
) -> tuple[str | None, str | None, bytes | None]:
    """Perform the real PaymentEscrow.charge + AttestationRegistry.seal calls.

    Returns (charge_tx, proof_tx, job_id); either tx may be None if that step
    failed, and job_id is None when the charge failed or was skipped.

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
        )
        return (None, None, None)

    try:
        settler = sc._signer_keypair().public_key
        auth_id = bytes.fromhex(auth_id_hex)
        job_id = secrets.token_bytes(16)

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
        if charge.get("status") == "SUCCESS" and charge_tx:
            settled_job_id = job_id
            await _emit(
                task_id,
                start,
                "cost",
                f"x402 charge → {total_usdc:.3f} USDC settled · tx {charge_tx[:10]}…",
            )
        else:
            logger.error(
                "task %s: PaymentEscrow.charge did not settle — status=%s hash=%s "
                "(auth %s, payer %s, %.6f USDC, job %s)",
                task_id,
                charge.get("status"),
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
                f"charge status={charge.get('status')} hash={charge_tx}",
            )
            return (charge_tx, None, None)

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
    except asyncio.CancelledError:
        # CancelledError is a BaseException, so the handler below never sees
        # it — yet a shutdown cancel (main.py's drain window) can land between
        # the charge submit and its confirmation, when the charge may still
        # settle on-chain. The reconstruction log must fire before the
        # cancellation propagates; cancellation semantics are preserved by
        # re-raising.
        logger.error(
            "task %s: on-chain settlement cancelled mid-flight "
            "(auth %s, payer %s, %.6f USDC, charge_tx=%s, proof_tx=%s)",
            task_id,
            auth_id_hex,
            payer,
            total_usdc,
            charge_tx,
            proof_tx,
        )
        raise
    except Exception as e:
        logger.error(
            "task %s: on-chain settlement failed: %s (auth %s, payer %s, %.6f USDC, charge_tx=%s, proof_tx=%s)",
            task_id,
            e,
            auth_id_hex,
            payer,
            total_usdc,
            charge_tx,
            proof_tx,
            exc_info=True,
        )
        await _emit(task_id, start, "error", "on-chain settlement failed")

    return (charge_tx, proof_tx, settled_job_id)


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

    Derived rather than random for the same reason `refund_svc.dispute_job_id`
    is: `ReputationLedger.submit` guards replay on `(agent_id, job_id)`, so a
    deterministic id means re-running the same task cannot double-count the
    same failure, while staying linkable to the run that produced it.
    """
    return hashlib.sha256(task_id.encode("utf-8") + b"unsettled").digest()[:16]


async def _submit_ratings(
    task_id: str,
    start: float,
    plan: StoredPlan,
    delivered: dict[str, Any],
    *,
    payer: str,
    job_id: bytes,
    undispatched: frozenset[str] = frozenset(),
    first_party_ids: frozenset[str] = frozenset(),
) -> None:
    """Submit the settler's synthetic per-step ratings to ReputationLedger.

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
    from . import reputation_svc

    # Sequential on purpose: parallel submits from the one scorer account
    # collide on sequence numbers (each tx consumes the account's next seq).
    for step in plan.plan.steps:
        # Keyed by agent_id — the one identity both worker kinds share. Worker
        # names do not: a bound external agent runs as "external.<agent_id>",
        # never as the operator's catalog agent_name, so a name lookup missed
        # every delivered external step and wrote a permanent on-chain 20/100
        # ("settled money for no delivered work") for an operator who shipped.
        # The agent_name fallback is for a caller that still hands in a
        # worker-name-keyed map, which is correct for a local step.
        if step.agent_id in undispatched:
            # We never sent them the step, so there is nothing to judge.
            continue
        step_output = delivered.get(step.agent_id)
        if step_output is None:
            step_output = delivered.get(step.agent_name or "")
        # Untrusted output must carry something checkable to earn the base
        # score. Without this an operator answering "{\"ok\": true}" forever
        # scored 70 — the prior exactly — and their lower bound ROSE with every
        # such reply, so lying outranked failing honestly (ADR 0005 D3).
        rating, weight = reputation_svc.synthetic_rating(
            step_output, step.est_price_usdc, first_party=step.agent_id in first_party_ids
        )
        try:
            # Async path: the submit RPC runs in a worker thread but the ~30s
            # status poll waits on the event loop — no executor thread pinned.
            result = await sc.submit_rating_async(
                step.agent_id,
                job_id,
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
                    "(job %s, rating %d, weight %d, payer %s)",
                    task_id,
                    step.agent_name,
                    step.agent_id,
                    status,
                    tx,
                    job_id.hex(),
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
            await _emit(
                task_id,
                start,
                "proof",
                f"reputation → {step.agent_name} rated {rating}/100 · tx {tx[:10]}…",
            )
        except Exception as e:
            logger.error(
                "task %s: reputation submit failed for %s (%s): %s (job %s, rating %d, weight %d, payer %s)",
                task_id,
                step.agent_name,
                step.agent_id,
                e,
                job_id.hex(),
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
