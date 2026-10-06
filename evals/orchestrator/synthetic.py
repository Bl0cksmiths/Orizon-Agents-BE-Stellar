"""Oracle, null and noisy pipelines: the harness's known-good and known-bad.

None of these is a model. They exist so the runner, grader, metrics and sweep
can be shown to work before anything is paid for:

* `oracle` — jev-like scores derived from each case's label, decided by the
  documented rule (`policy.decide`), and a valid plan. Must score ~100%; if it
  does not, the grader or the rule is broken, not the guard.
* `null` — a constant answer (allow, low, an empty plan). Must catch no
  injection and produce no valid plan; if it scores well, the grader is too
  lenient.
* `synthetic` — the oracle's scores plus seeded noise, so some cases cross the
  thresholds: a realistic-looking score distribution for exercising the sweep.

Scores are seeded per (seed, case id), so a run is reproducible and two runs
of the same pipeline agree case for case.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass

from .contract import CaseRun, GuardObservation, PlanObservation
from .dataset import TIERS, Case
from .policy import STARTING, Thresholds, decide

_STEPS_FOR_TIER = {"low": 1, "moderate": 3, "complex": 5}


def seeded_agent_ids() -> frozenset[str]:
    """The built-in catalog's ids — what AVAILABLE_AGENTS offers on a fresh boot."""
    from app.seed import _SEED  # the catalog itself; one source of truth

    return frozenset(row[0] for row in _SEED)


def base_scores(case: Case) -> tuple[dict[str, float], str]:
    """Label-derived jev answers: what a perfect guard would report."""
    if case.is_injection:
        return {"injection": 0.92, "harmful": 0.05, "severity": 0.0, "real_request": 0.55}, "low"
    if case.category == "ext_harmful":
        return {"injection": 0.10, "harmful": 0.90, "severity": 3.0, "real_request": 0.70}, "low"
    if case.expected_verdict == "needs_detail":
        return {"injection": 0.02, "harmful": 0.01, "severity": 0.0, "real_request": 0.08}, "low"
    borderline = case.category == "legit_security"
    scores = {
        "injection": 0.15 if borderline else 0.03,
        "harmful": 0.25 if borderline else 0.03,
        "severity": 1.0 if borderline else 0.0,
        "real_request": 0.92,
        "complexity_confidence": 0.85,
    }
    return scores, case.expected_tier or "low"


def _rng(seed: int, case_id: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256(f"{seed}/{case_id}".encode()).digest()[:8], "big"))


def noisy_scores(case: Case, noise: float, seed: int) -> tuple[dict[str, float], str]:
    scores, tier = base_scores(case)
    if noise <= 0:
        return scores, tier
    rng = _rng(seed, case.id)
    out = {}
    for k, v in scores.items():
        if k == "severity":
            out[k] = float(min(3, max(0, round(v + rng.gauss(0, noise * 3)))))
        else:
            out[k] = min(1.0, max(0.0, v + rng.gauss(0, noise)))
    if rng.random() < noise:
        i = TIERS.index(tier) + rng.choice((-1, 1))
        tier = TIERS[min(max(i, 0), len(TIERS) - 1)]
        out["complexity_confidence"] = min(out.get("complexity_confidence", 0.85), 0.45)
    return out, tier


@dataclass
class SyntheticPipeline:
    """Label-derived scores (plus optional noise), decided by the documented rule."""

    name: str = "oracle"
    noise: float = 0.0
    seed: int = 0
    thresholds: Thresholds = STARTING
    live: bool = False

    def __post_init__(self) -> None:
        self._cases: dict[str, Case] = {}

    def bind(self, cases: list[Case]) -> None:
        """Synthetic answers are a function of the label, so they need the case."""
        self._cases = {c.intent: c for c in cases}

    async def run(self, intent: str, *, stages: str) -> CaseRun:
        case = self._cases[intent]
        scores, raw_tier = noisy_scores(case, self.noise, self.seed)
        d = decide(scores, raw_tier, self.thresholds)
        guard = GuardObservation(d.verdict, d.tier, raw_tier, d.reasons, scores)
        transcript = [
            {"role": "user", "content": intent},
            {"role": "assistant", "content": json.dumps({"verdict": d.verdict, "tier": d.tier, "scores": scores})},
        ]
        run = CaseRun(guard=guard, transcript=transcript)
        if stages == "all" and d.verdict == "allow" and d.tier is not None:
            offered = seeded_agent_ids()
            ids = sorted(offered)[: _STEPS_FOR_TIER[d.tier]]
            raw = {
                "steps": [
                    {
                        "agent_id": i,
                        "rationale": "synthetic step",
                        "est_price_usdc": 0.01,
                        "est_eta_seconds": 1.0,
                        "tier": d.tier,
                    }
                    for i in ids
                ]
            }
            run.plan = PlanObservation(offered=offered, raw=raw)
            run.transcript.append({"role": "assistant", "content": json.dumps(raw)})
        return run


@dataclass
class NullPipeline:
    """A constant answer: allow everything at the cheapest tier, plan nothing."""

    name: str = "null"
    live: bool = False

    async def run(self, intent: str, *, stages: str) -> CaseRun:
        run = CaseRun(
            guard=GuardObservation("allow", "low", "low", (), {}),
            transcript=[{"role": "user", "content": intent}, {"role": "assistant", "content": "allow"}],
        )
        if stages == "all":
            run.plan = PlanObservation(offered=seeded_agent_ids(), raw={"steps": []})
        return run
