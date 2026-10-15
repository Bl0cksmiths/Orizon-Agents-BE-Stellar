"""Characterization tests for the worker sample's run loop (`run_sample`).

The registry's workers are replaced with scripted stand-ins that make their
calls through the app's Claude layer (on FakeClaude), so the real recording
and budget transports book them. The clock is a counter, so every latency in
a record is a known number and whole records can be compared.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from app.agents import registry
from app.llm import claude, testing
from evals.orchestrator import workers_sample
from evals.orchestrator.workers_sample import BudgetExceeded, Job, run_sample


class _Worker:
    """A worker that makes `calls` text calls, then returns `out` or raises `exc`."""

    def __init__(self, name: str, out: dict[str, Any] | None = None, exc: Exception | None = None, calls: int = 1):
        self.name = name
        self.out = out or {}
        self.exc = exc
        self.calls = calls
        self.seen: list[tuple[str, str, dict[str, Any], str | None]] = []

    async def run(self, intent: str, rationale: str, context: dict[str, Any], tier: str | None = None) -> Any:
        self.seen.append((intent, rationale, context, tier))
        for _ in range(self.calls):
            await claude.text(
                purpose=f"worker.{self.name}", model="claude-sonnet-5-5", system="s", user="u", max_tokens=9
            )
        if self.exc is not None:
            raise self.exc
        return self.out


class _StreamAborted:
    """A worker whose streamed reply is aborted mid-stream from outside."""

    name = "code.gen"

    async def run(self, intent: str, rationale: str, context: dict[str, Any], tier: str | None = None) -> Any:
        def abort(delta: str) -> None:
            raise RuntimeError("wall clock")

        await claude.text(
            purpose="worker.code.gen",
            model="claude-sonnet-5-5",
            system="s" * 350,
            user="u",
            max_tokens=9,
            stream=True,
            on_text=abort,
        )
        return {}


def _job(agent_id: str, **kw: Any) -> Job:
    base: dict[str, Any] = {
        "agent_id": agent_id,
        "case_id": None,
        "intent": f"intent for {agent_id}",
        "rationale": "why",
        "max_tokens": 9,
        "model": "claude-sonnet-5-5",
    }
    base.update(kw)
    return Job(**base)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = itertools.count()
    monkeypatch.setattr(workers_sample.time, "perf_counter", lambda: float(next(ticks)))


def _install(monkeypatch: pytest.MonkeyPatch, fake: testing.FakeClaude, workers: dict[str, Any]) -> None:
    monkeypatch.setattr(registry, "WORKERS", workers)
    claude.set_transport(fake)


def _files(out: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(out.iterdir())}


def test_an_unbudgeted_sample_records_every_job_and_feeds_the_critic(monkeypatch, tmp_path, clock):
    draft = {
        "summary": "a page",
        "counts": {"lines": 3},
        "artifact": {"title": "Page", "files": [{"path": "site/main.html", "content": "<p>hi</p>"}, {}]},
        "validator_violations": [],
        "hero": "h",
        "bulk": "x" * 50,
    }
    gen = _Worker("code.gen", out=draft)
    critic = _Worker("code.critic", out={"summary": "polished", "critic_violations": ["v"]}, calls=2)
    fake = testing.FakeClaude()
    for text in ("g", "c1", "c2"):
        fake.reply(text)
    _install(monkeypatch, fake, {"agt_11c0": gen, "agt_12r0": critic})
    jobs = (
        _job("agt_11c0", case_id="cpx-001", label="code.gen#1", tier="complex"),
        _job("agt_12r0", critic_of="code.gen#1"),
    )
    results = asyncio.run(run_sample(tmp_path, jobs))

    assert list(results) == ["code.gen#1", "code.critic"]
    assert gen.seen == [("intent for agt_11c0", "why", {}, "complex")]
    assert critic.seen == [("intent for agt_12r0", "why", {"code.gen": draft}, None)]
    files = _files(tmp_path)
    assert list(files) == ["code_critic.json", "code_gen_1.json", "code_gen_1__index.html", "code_gen_1__main.html"]
    assert files["code_gen_1__main.html"] == "<p>hi</p>"
    assert files["code_gen_1__index.html"] == ""  # a file with no path or content
    gen_record = json.loads(files["code_gen_1.json"])
    assert gen_record == json.loads(json.dumps(results["code.gen#1"]))
    call = gen_record["calls"][0]
    # The clock is a counter, so the latencies are whole numbers of ticks.
    assert gen_record.pop("latency_s") > 0
    assert call.pop("latency_ms") > 0
    assert gen_record == {
        "agent_id": "agt_11c0",
        "worker": "code.gen",
        "label": "code.gen#1",
        "unreported_estimated_usd": 0.0,
        "case_id": "cpx-001",
        "tier": "complex",
        "intent": "intent for agt_11c0",
        "cost_usd": call["cost_usd"],
        "calls": [
            {
                "model": "claude-sonnet-5-5",
                "response_model": "claude-sonnet-5-5",
                "served_by": None,
                "input_tokens": testing.DEFAULT_USAGE.input_tokens,
                "output_tokens": testing.DEFAULT_USAGE.output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cost_usd": call["cost_usd"],
                "app_cost_usd": call["app_cost_usd"],
                "first_token_ms": None,
                "effort": "medium",
                "stop_reason": "end_turn",
            }
        ],
        "error": None,
        "output": {
            "summary": "a page",
            "counts": {"lines": 3},
            "artifact_title": "Page",
            "artifact_files": ["site/main.html", None],
            "validator_violations": [],
            "hero": "h",
        },
    }
    assert call["cost_usd"] > 0
    critic_record = results["code.critic"]
    assert critic_record["label"] == "code.critic"
    assert len(critic_record["calls"]) == 2
    assert critic_record["output"] == {"summary": "polished", "counts": None, "critic_violations": ["v"]}
    assert files["code_critic.json"] == json.dumps(critic_record, indent=2, ensure_ascii=False) + "\n"


def test_a_job_that_does_not_fit_the_budget_is_skipped(monkeypatch, tmp_path, clock):
    worker = _Worker("copywrite")
    fake = testing.FakeClaude().reply("a")
    _install(monkeypatch, fake, {"agt_01h8": worker})
    jobs = (_job("agt_01h8", typical_usd=0.009), _job("agt_01h8", label="copywrite#2", typical_usd=0.009))
    results = asyncio.run(run_sample(tmp_path, jobs, budget_usd=0.013))
    first, second = results["copywrite"], results["copywrite#2"]
    assert first["error"] is None
    assert first["cost_usd"] > 0
    left = 0.013 - first["cost_usd"]
    assert second == {"worker": "copywrite", "label": "copywrite#2", "skipped": f"budget left {left:.4f} USD"}
    assert _files(tmp_path)["copywrite_2.json"] == json.dumps(second, indent=2) + "\n"
    assert len(worker.seen) == 1


def test_a_budget_cut_off_is_booked_as_unreported_spend(monkeypatch, tmp_path, clock):
    cut = _Worker("code.gen", exc=BudgetExceeded(0.25), calls=0)
    after = _Worker("copywrite")
    _install(monkeypatch, testing.FakeClaude().reply("a"), {"agt_11c0": cut, "agt_01h8": after})
    jobs = (_job("agt_11c0"), _job("agt_01h8", typical_usd=0.61))
    results = asyncio.run(run_sample(tmp_path, jobs, budget_usd=1.0))
    rec = results["code.gen"]
    assert rec["error"] == "BudgetExceeded: stopped at an estimated $0.2500"
    assert rec["unreported_estimated_usd"] == 0.25
    assert rec["calls"] == []
    assert rec["cost_usd"] == 0
    assert rec["output"] == {"summary": None, "counts": None}
    # 1.0 - 0.25 left, and 0.61 x 1.25 headroom does not fit
    assert results["copywrite"]["skipped"] == "budget left 0.7500 USD"


def test_a_stream_aborted_from_outside_books_its_running_estimate(monkeypatch, tmp_path, clock):
    _install(monkeypatch, testing.FakeClaude().reply("x" * 30), {"agt_11c0": _StreamAborted()})
    results = asyncio.run(run_sample(tmp_path, (_job("agt_11c0"),), budget_usd=1.0))
    rec = results["code.gen"]
    unreported = rec["unreported_estimated_usd"]
    assert unreported > 0
    assert rec["error"] == f"RuntimeError: wall clock (stream aborted; estimated ${unreported:.4f} unreported)"


def test_a_worker_failure_without_a_budget_is_a_plain_error(monkeypatch, tmp_path, clock):
    _install(monkeypatch, testing.FakeClaude().reply("a"), {"agt_01h8": _Worker("copywrite", exc=ValueError("bad"))})
    results = asyncio.run(run_sample(tmp_path, (_job("agt_01h8"),)))
    rec = results["copywrite"]
    assert rec["error"] == "ValueError: bad"
    assert rec["unreported_estimated_usd"] == 0.0
    assert len(rec["calls"]) == 1
