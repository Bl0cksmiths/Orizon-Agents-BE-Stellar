"""Story 2.03's acceptance criteria, driven end to end through the run loop.

Recon found these two were argued rather than asserted:

  * **AC-1** — nothing pinned that a TIMED-OUT step is excluded from the charge.
    The existing coverage used a raising worker or asserted logging only, and
    the AC specifically describes a timeout alongside two successes.
  * **AC-3** — three failure classes had to be *distinguishable*, and twelve of
    them rendered to the buyer as the same line. The classes are now surfaced,
    so the distinction is assertable rather than a matter of reading prose in a
    server log.
  * **The premise itself** — "non-delivery has a cost to the agent". A PARTIAL
    run rated nobody, because the submit sat behind `charge_tx and job_id` and
    `PaymentEscrow.charge` cannot settle. Nothing pinned that, so the mechanism
    could be dead in production with a green suite.

The helpers mirror `tests/test_external_step_hardening.py` deliberately: the
dispatch seam is pinned and every other line of the run loop is real.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.workers.external_http import ExternalDispatchError
from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.state import state
from app.stellar import client as sc

INTENT = "ship the launch page"
PRICE = 0.02
AUTH = "ab" * 16
PAYER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
GOOD = {"summary": "done", "artifact": {"title": "t", "files": [{"path": "a.html", "content": "x"}]}}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    yield
    for tid in [t for t in state.tasks if t.startswith("tsk_ac_")]:
        state.tasks.pop(tid, None)
        state.traces.pop(tid, None)


class _Answers:
    real = True

    def __init__(self, name: str, output: Any = None, raises: BaseException | None = None) -> None:
        self.name = name
        self._output = output
        self._raises = raises

    async def run(self, intent: str, rationale: str, context: dict | None = None) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._output


def _resolves_to(monkeypatch, workers: dict[str, Any]) -> None:
    async def _resolve(agent_id: str) -> Any:
        return workers.get(agent_id)

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)


def _plan(plan_id: str, *steps: str) -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent=INTENT,
        plan=Plan(
            steps=[
                PlanStep(agent_id=a, agent_name=a, rationale="do it", est_price_usdc=PRICE, est_eta_seconds=1.0)
                for a in steps
            ]
        ),
        total_usdc=PRICE * len(steps),
        total_eta=1.0,
    )


def _add_task(task_id: str, steps: int) -> None:
    state.add_task(Task(id=task_id, intent=INTENT, agents=steps, spent=0.0, status="running"))


def _settled_total(monkeypatch) -> list[float]:
    """Record what the charge was actually asked to settle."""
    totals: list[float] = []

    async def fake_settle(task_id, start, plan, *, payer, auth_id_hex, total_usdc):
        totals.append(total_usdc)
        return ("chargehash", "sealhash", b"\x02" * 16)

    async def fake_ratings(*a, **k):
        return None

    monkeypatch.setattr(execution_svc, "_settle_onchain", fake_settle)
    monkeypatch.setattr(execution_svc, "_submit_ratings", fake_ratings)
    return totals


def _trace(task_id: str) -> list[str]:
    return [ln.msg for ln in state.traces[task_id]]


# ── AC-1 — a timed-out external step is not charged ─────────────────


def test_a_timed_out_external_step_is_excluded_from_the_charge(monkeypatch):
    """Two local steps deliver, one external step times out. The charge must
    equal the two delivered steps exactly — the buyer never pays for a step
    that produced nothing."""
    monkeypatch.setattr(execution_svc, "STEP_TIMEOUT_SECONDS", 0.05)
    totals = _settled_total(monkeypatch)

    class _Hangs:
        real = True
        name = "external.ext_slow"

        async def run(self, intent, rationale, context=None):
            await asyncio.sleep(5)

    _resolves_to(
        monkeypatch,
        {"agt_a": _Answers("w.a", GOOD), "ext_slow": _Hangs(), "agt_b": _Answers("w.b", GOOD)},
    )
    task_id = "tsk_ac_timeout"
    _add_task(task_id, 3)

    asyncio.run(
        execution_svc._run(
            _plan("pln_ac_timeout", "agt_a", "ext_slow", "agt_b"), task_id, auth_id_hex=AUTH, payer=PAYER
        )
    )

    assert totals == [pytest.approx(PRICE * 2)]
    assert state.tasks[task_id].spent == pytest.approx(PRICE * 2)


# ── AC-3 — failure classes are distinguishable ──────────────────────


def test_three_failure_classes_read_differently_in_the_trace(monkeypatch):
    """Timeout, connection refusal and schema rejection are three different
    operator remediations — answer faster, come up, send the documented shape.
    Before 2.03 all three rendered as the identical line."""
    _settled_total(monkeypatch)
    _resolves_to(
        monkeypatch,
        {
            "ext_slow": _Answers(
                "external.ext_slow", raises=ExternalDispatchError("response_timeout", "no response within 100s")
            ),
            "ext_down": _Answers(
                "external.ext_down", raises=ExternalDispatchError("no_connection", "no connection after retry")
            ),
            "ext_junk": _Answers(
                "external.ext_junk", raises=ExternalDispatchError("invalid_response", "summary_missing")
            ),
        },
    )
    task_id = "tsk_ac_classes"
    _add_task(task_id, 3)

    asyncio.run(
        execution_svc._run(
            _plan("pln_ac_classes", "ext_slow", "ext_down", "ext_junk"), task_id, auth_id_hex=AUTH, payer=PAYER
        )
    )

    lines = [ln for ln in _trace(task_id) if "failed" in ln]
    assert len(lines) == 3
    assert len(set(lines)) == 3, f"failure classes are indistinguishable: {lines}"
    assert any("response_timeout" in ln for ln in lines)
    assert any("no_connection" in ln for ln in lines)
    assert any("invalid_response" in ln for ln in lines)


def test_an_unclassified_failure_still_traces_without_crashing(monkeypatch):
    """The run loop reads the class duck-typed, so a worker that raises a plain
    exception must degrade to a generic token rather than break the classifier."""
    _settled_total(monkeypatch)
    _resolves_to(monkeypatch, {"agt_x": _Answers("w.x", raises=RuntimeError("no rule attribute"))})
    task_id = "tsk_ac_unclassified"
    _add_task(task_id, 1)

    asyncio.run(execution_svc._run(_plan("pln_ac_unclassified", "agt_x"), task_id, auth_id_hex=AUTH, payer=PAYER))

    assert any("unclassified" in ln for ln in _trace(task_id))


def test_the_trace_class_cannot_carry_operator_text(monkeypatch):
    """The class is a token from a closed vocabulary, so a hostile rule value
    cannot smuggle a URL or a key into a world-readable trace line."""
    _settled_total(monkeypatch)
    hostile = ExternalDispatchError("no_connection", "x")
    object.__setattr__(hostile, "rule", "https://evil.example/?token=SECRET")
    _resolves_to(monkeypatch, {"agt_x": _Answers("w.x", raises=hostile)})
    task_id = "tsk_ac_leak"
    _add_task(task_id, 1)

    asyncio.run(execution_svc._run(_plan("pln_ac_leak", "agt_x"), task_id, auth_id_hex=AUTH, payer=PAYER))

    assert not any("SECRET" in ln for ln in _trace(task_id))


# ── the premise — a partial run costs the agent that did not deliver ──

# What `synthetic_rating` pays a step that produced nothing: timed out, raised,
# or answered with nothing checkable. The whole mechanism is this number
# landing on the right agent.
FAILED = 20


def _rates(monkeypatch, settle_result: tuple[str | None, str | None, bytes | None]) -> list[tuple[str, bytes, int]]:
    """Put the run on the on-chain path and record every rating it submits.

    Only the two calls that leave the process are replaced — the charge/seal,
    which returns `settle_result`, and the ledger submit. `_submit_ratings`
    and the branch that decides whether to call it are the code under test.
    """
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_signing_key", "SFAKEKEY")

    async def fake_settle(task_id, start, plan, *, payer, auth_id_hex, total_usdc):
        return settle_result

    monkeypatch.setattr(execution_svc, "_settle_onchain", fake_settle)

    calls: list[tuple[str, bytes, int]] = []

    async def fake_submit(agent_id, job_id, rating, weight, payer, kind="auto"):
        calls.append((agent_id, job_id, rating))
        return {"hash": "deadbeefcafe0123", "status": "SUCCESS"}

    monkeypatch.setattr(sc, "submit_rating_async", fake_submit)
    return calls


# The two ways the charge declines to hand back a job id. Both are live: a
# charge that raises returns the first, and one that comes back non-SUCCESS
# returns the second — and the deployed PaymentEscrow.charge cannot reach
# either's happy path, because authorize() takes no custody, so charge's
# transfer(&auth.payer, …) wants a require_auth only the payer can give.
@pytest.mark.parametrize(
    "settle_result",
    [(None, None, None), ("chargehash", None, None)],
    ids=["charge_raised", "charge_not_success"],
)
def test_a_partial_run_rates_both_outcomes_when_nothing_settled(monkeypatch, settle_result):
    """One agent delivered, one was asked and did not. Both get rated.

    This is story 2.03's premise, and until now it was false for every run that
    reached the charge: the submit was gated on `charge_tx and job_id`, so a
    broken endpoint kept its prior and stayed routable forever — while the
    operator who DID deliver earned no positive evidence either.
    """
    calls = _rates(monkeypatch, settle_result)
    _resolves_to(
        monkeypatch,
        {
            "agt_a": _Answers("w.a", GOOD),
            "ext_down": _Answers("external.ext_down", raises=ExternalDispatchError("no_connection", "refused")),
        },
    )
    task_id = "tsk_ac_partial_unsettled"
    _add_task(task_id, 2)

    asyncio.run(
        execution_svc._run(
            _plan("pln_ac_partial_unsettled", "agt_a", "ext_down"), task_id, auth_id_hex=AUTH, payer=PAYER
        )
    )

    rated = {agent: rating for agent, _job, rating in calls}
    assert rated == {"agt_a": 85, "ext_down": FAILED}
    # Under the task-derived id, which is deterministic — the ledger rejects a
    # second rating for the same (agent_id, job_id), so re-running this task
    # cannot double-count the same failure.
    assert {job for _agent, job, _rating in calls} == {execution_svc.unsettled_job_id(task_id)}


def test_a_settled_partial_run_still_rates_under_the_charges_job_id(monkeypatch):
    """The fallback is a fallback. When the charge did mint a job id, the
    ratings stay linked to the payment that funded the run rather than to a
    second, synthetic id for the same steps."""
    calls = _rates(monkeypatch, ("chargehash", "sealhash", b"\x02" * 16))
    _resolves_to(
        monkeypatch,
        {
            "agt_a": _Answers("w.a", GOOD),
            "ext_down": _Answers("external.ext_down", raises=ExternalDispatchError("no_connection", "refused")),
        },
    )
    task_id = "tsk_ac_partial_settled"
    _add_task(task_id, 2)

    asyncio.run(
        execution_svc._run(_plan("pln_ac_partial_settled", "agt_a", "ext_down"), task_id, auth_id_hex=AUTH, payer=PAYER)
    )

    assert {job for _agent, job, _rating in calls} == {b"\x02" * 16}
    assert execution_svc.unsettled_job_id(task_id) not in {job for _agent, job, _rating in calls}


def test_an_agent_we_never_reached_is_rated_by_nobody_on_an_unsettled_run(monkeypatch):
    """Decoupling ratings from settlement must not widen who gets rated.

    `resolve_worker` fails OPEN, so an unreadable binding store returns None
    exactly like a missing binding. An agent we never dispatched to did not
    fail to deliver — it was never asked, and our outage is not their
    reputation (ADR 0005 D5).
    """
    calls = _rates(monkeypatch, (None, None, None))
    _resolves_to(
        monkeypatch,
        {
            "agt_a": _Answers("w.a", GOOD),
            "ext_down": _Answers("external.ext_down", raises=ExternalDispatchError("no_connection", "refused")),
        },
    )
    task_id = "tsk_ac_partial_ghost"
    _add_task(task_id, 3)

    asyncio.run(
        execution_svc._run(
            _plan("pln_ac_partial_ghost", "agt_a", "ext_down", "ext_ghost"),
            task_id,
            auth_id_hex=AUTH,
            payer=PAYER,
        )
    )

    assert [agent for agent, _job, _rating in calls] == ["agt_a", "ext_down"]
