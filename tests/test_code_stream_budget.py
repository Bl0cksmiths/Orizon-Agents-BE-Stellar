"""code.gen and code.critic on Claude finish inside the step deadline, or fail cleanly.

The live re-check of 2026-10-06 (evals/orchestrator/reports/2026-10-06-recheck/)
watched code.gen on a complex request, capped to Claude Sonnet 5.5, still
streaming at 104.8 s — it would have run into the run loop's 120 s step
deadline, which fails the step as `step_timeout` and rates it against the
agent. Measured earlier: Sonnet writes 282–355-line apps in 35–45 s, about
110 tokens/s including thinking.

So, on the Claude path only (the agno prompts stay byte-identical):
  * the prompts ask for about 250–450 lines, core features first, with
    deferred features named in the summary;
  * the output ceiling is sized to the deadline (tokens / ~100 tok/s plus
    first-token latency stays under ~95 s), at effort "low";
  * a client-side wall-clock budget aborts a stream still running at 100 s and
    fails the step as `model_truncated` — unbilled, and before the deadline.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

from app.agents.registry import WORKERS
from app.agents.workers import claude_step, code_critic, code_gen
from app.agents.workers.claude_step import ModelStepError
from app.config import settings
from app.llm import claude as claude_layer
from app.llm.claude import ClaudeRequest, Completion
from app.llm.testing import FakeClaude
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.state import state

APP = "<!doctype html><html><head><title>t</title></head><body><main>ok</main></body></html>"
TAGGED = f"<artifact_title>T</artifact_title><artifact_summary>s</artifact_summary><artifact_html>{APP}</artifact_html>"

# sha256 of the agno-path prompts as they stood before orchestrator v2 (7ca35d6).
AGNO_CODE_GEN = "2d4367206271f12f13a955f45da992bf40a897bbdea9f3384c27a85c63f7eb2a"
AGNO_CODE_CRITIC = "d081bb9551069a0e5acfb3028c8546a6b5c97a7c433b7df95aa6a5c403d9927f"

# What the ceiling is sized on. The live re-measure of 2026-10-06
# (evals/orchestrator/reports/2026-10-06-recheck/r3-code-length/) clocked
# Sonnet 5.5 at low effort at about 180–220 output tokens/s (code.gen: 7 058
# tokens in 39.2 s, first token at 1.9 s). The ceiling must finish at the
# SLOWEST measured rate with first-token latency doubled and 15% of the stream
# budget still to spare; the budget itself guards anything slower.
SLOWEST_MEASURED_TOKENS_PER_SECOND = 180.0
FIRST_TOKEN_SECONDS = 2 * 1.9
BUDGET_SHARE = 0.85


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def _critic_context() -> dict[str, Any]:
    art = code_gen.CodeGen()._artifact_dict(code_gen.parse_tagged_artifact(TAGGED))
    return {"code.gen": {"artifact": art}}


CODE_STEPS = [("agt_11c0", None), ("agt_12r0", "critic")]


def _run(agent_id: str, context: Any, tier: Any = None) -> dict[str, Any]:
    ctx = _critic_context() if context else None
    return asyncio.run(WORKERS[agent_id].run("a barbershop booking system", "r", context=ctx, tier=tier))


# ── the prompts ─────────────────────────────────────────────────────────────


def test_the_agno_prompts_are_byte_identical() -> None:
    assert hashlib.sha256(code_gen.INSTRUCTIONS.encode()).hexdigest() == AGNO_CODE_GEN
    assert hashlib.sha256(code_critic.INSTRUCTIONS.encode()).hexdigest() == AGNO_CODE_CRITIC


@pytest.mark.parametrize(
    "prompt", [code_gen.CLAUDE_INSTRUCTIONS, code_critic.CLAUDE_INSTRUCTIONS], ids=["gen", "critic"]
)
def test_the_claude_prompts_ask_for_a_bounded_app_core_first(prompt: str) -> None:
    flat = " ".join(prompt.split()).casefold()
    assert "about 250–450 lines" in flat
    assert "prioritise working core features over breadth" in flat
    assert "list the deferred features in the summary" in flat
    # Readable source, so the line count reflects the work done.
    assert "one statement or declaration per line" in flat
    assert "no minified css or js" in flat
    # The agno length targets would contradict it, so they are not in it.
    for longer in ("400–700", "600–1000", "500–900"):
        assert longer not in flat


# ── ceiling and effort ──────────────────────────────────────────────────────


@pytest.mark.parametrize("module", [code_gen, code_critic], ids=["gen", "critic"])
def test_the_output_ceiling_fits_inside_the_stream_budget(module: Any) -> None:
    seconds = module.MAX_TOKENS / SLOWEST_MEASURED_TOKENS_PER_SECOND + FIRST_TOKEN_SECONDS
    assert seconds <= claude_step.STREAM_BUDGET_SECONDS * BUDGET_SHARE
    # …and leaves room for a 450-line app written readably (~40 characters a
    # line) at the measured density: 14 385 characters for 7 058 tokens,
    # thinking included.
    assert module.MAX_TOKENS >= 450 * 40 / (14_385 / 7_058)


def test_the_ceilings_are_the_measured_ones() -> None:
    """code.critic reads the draft and rewrites it whole, so it gets the
    larger ceiling (it used 93% of the old 9 000 in the live re-measure)."""
    assert (code_gen.MAX_TOKENS, code_critic.MAX_TOKENS) == (12_000, 14_000)


@pytest.mark.parametrize(("agent_id", "context"), CODE_STEPS)
@pytest.mark.parametrize("tier", ["moderate", "complex", None])
def test_code_steps_stream_at_low_effort_under_the_ceiling(
    agent_id: str, context: Any, tier: Any, claude: FakeClaude
) -> None:
    claude.reply(TAGGED)
    _run(agent_id, context, tier)
    call = claude.calls[0]
    assert (call.model, call.effort, call.stream) == ("claude-sonnet-5-5", "low", True)
    assert call.max_tokens == (code_gen.MAX_TOKENS if agent_id == "agt_11c0" else code_critic.MAX_TOKENS)


def test_a_reply_cut_at_the_ceiling_still_fails_as_truncated(claude: FakeClaude) -> None:
    claude.truncate(partial="<artifact_title>T</artifact_title><artifact_html><!doctype html><html>")
    with pytest.raises(ModelStepError) as info:
        _run("agt_11c0", None)
    assert info.value.rule == "model_truncated"


# ── the wall-clock budget ───────────────────────────────────────────────────


class _SlowStream:
    """A Claude that is still writing when the budget runs out."""

    def __init__(self) -> None:
        self.cancelled = False

    async def complete(self, request: ClaudeRequest) -> Completion:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


def test_the_stream_budget_sits_inside_the_step_deadline() -> None:
    assert claude_step.STREAM_BUDGET_SECONDS <= execution_svc.STEP_TIMEOUT_SECONDS - 15


@pytest.mark.parametrize(("agent_id", "context"), CODE_STEPS)
def test_a_stream_over_budget_is_aborted_as_truncated(
    agent_id: str, context: Any, claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_step, "STREAM_BUDGET_SECONDS", 0.05)
    slow = _SlowStream()
    claude_layer.set_transport(slow)
    with pytest.raises(ModelStepError) as info:
        _run(agent_id, context)
    assert info.value.rule == "model_truncated"
    assert slow.cancelled, "the stream must be aborted, not left running"


def test_the_run_loop_sees_a_truncated_step_not_a_timed_out_one(
    claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget fires first, so the step fails as `model_truncated` (unbilled)
    rather than reaching the loop's deadline as `step_timeout`."""
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)
    monkeypatch.setattr(claude_step, "STREAM_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(execution_svc, "STEP_TIMEOUT_SECONDS", 2.0)
    claude_layer.set_transport(_SlowStream())
    step = PlanStep(agent_id="agt_11c0", agent_name="code.gen", rationale="r", est_price_usdc=0.1, est_eta_seconds=1.0)
    plan = StoredPlan(
        id="pln_tsk_budget", intent="a booking system", plan=Plan(steps=[step]), total_usdc=0.1, total_eta=1
    )
    state.add_task(Task(id="tsk_budget", intent="a booking system", agents=1, spent=0.0, status="running"))
    try:
        asyncio.run(execution_svc._run(plan, "tsk_budget"))
        trace = [line.msg for line in state.traces["tsk_budget"]]
        assert "code.gen failed (model_truncated)" in trace
        assert "code.gen timed out" not in trace
        assert state.tasks["tsk_budget"].spent == 0.0
    finally:
        state.tasks.pop("tsk_budget", None)
        state.traces.pop("tsk_budget", None)


def test_structured_workers_have_no_stream_budget(claude: FakeClaude, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the two streamed code workers are bounded this way; a short
    structured call is left to the step deadline as before."""
    monkeypatch.setattr(claude_step, "STREAM_BUDGET_SECONDS", 0.0)
    claude.reply({"keywords": ["k"], "audiences": ["a"], "summary": "s"})
    asyncio.run(WORKERS["agt_05x7"].run("x", "r"))
    assert claude.calls
