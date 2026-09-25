"""One agent hired for two steps of one plan must leave two ratings, each of
its own step's work.

`ReputationLedger.submit` guards on `Rated(agent_id, job_id)` and answers
`Error::Replay` BEFORE it reads `kind` — the R12 collision story 4.04 removed
from the DISPUTE path (ADR 0009 D1) and the settlement path kept. Two things
followed from it, and a plan the orchestrator is free to emit triggered both:

  * every step of a run was submitted under the run's one job id, so the second
    step served by an agent that already had a rating for that job was refused
    at simulation and reported as "reputation submit failed"; and
  * `delivered` was keyed by agent_id, so that agent's two outputs shared one
    slot and BOTH its steps were graded on whichever landed last.

Half the run's reputation evidence, lost — and the surviving half attached to
the wrong work. The fake ledger below is that guard, checked in the order the
contract checks it, so a rating the chain would refuse is refused here too.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import pytest

from app.config import settings
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.state import state
from app.stellar import client as sc

INTENT = "draft it, then critique it"
PRICE = 0.02
AUTH = "ab" * 16
PAYER = "G" + "A" * 55
# The job id a successful charge mints, as `_settle_onchain` hands it back.
JOB_ID = b"\x3c" * 16

# `Error::Replay`, numbered as USING_CONTRACTS.md and rating_writer.LEDGER_ERRORS
# name it. Spelled here rather than imported so this file models the CONTRACT,
# not the service's reading of it.
LEDGER_REPLAY = 7

# Two answers from one endpoint, scored apart on purpose (ADR 0005 D3 rates
# untrusted output on what it can check): an artifact with a clean critic pass
# is 70 + 15 + 10, the same artifact with two violations is 70 + 15 - 6. Both
# carry checkable work, so neither is the trivial "delivered nothing" case —
# the ratings differ only because the OUTPUTS do.
CLEAN = {"artifact": {"title": "draft", "files": []}, "critic_violations": []}
FLAWED = {"artifact": {"title": "critique", "files": []}, "critic_violations": ["unbounded loop", "no tests"]}
CLEAN_RATING = 95
FLAWED_RATING = 79


@pytest.fixture(autouse=True)
def clean_tasks():
    yield
    for task_id in [t for t in state.tasks if t.startswith("tsk_ratingid_")]:
        state.tasks.pop(task_id, None)
    for task_id in [t for t in state.traces if t.startswith("tsk_ratingid_")]:
        state.traces.pop(task_id, None)


class _FakeLedger:
    """`ReputationLedger.submit`, reduced to the one rule this file is about.

    The guard is `Rated(agent_id, job_id)`, it lives in persistent storage with
    no entrypoint that removes it, and it is checked BEFORE `kind` is read — so
    a pair is spent for ever, whatever kind of rating spent it. A hit is
    `Error::Replay` at simulation, before any transaction exists, which is why
    this raises `ContractError` instead of returning a hash and a status.
    """

    def __init__(self) -> None:
        self.rated: dict[tuple[str, bytes], tuple[int, str]] = {}
        self.refused: list[tuple[str, bytes]] = []

    async def submit(
        self,
        agent_id: str,
        job_id: bytes,
        rating: int,
        weight: int,
        payer: str,
        kind: str = "auto",
    ) -> dict[str, Any]:
        key = (agent_id, job_id)
        if key in self.rated:
            self.refused.append(key)
            raise sc.ContractError(f"prepare failed: HostError: Error(Contract, #{LEDGER_REPLAY}) …", LEDGER_REPLAY)
        self.rated[key] = (rating, kind)
        return {"hash": "deadbeefcafe0123", "status": "SUCCESS"}


class _AnswersInTurn:
    """One bound endpoint, answering each dispatch with the next output.

    The same agent on two steps is the same endpoint called twice, and a
    critique is not the draft it critiques — so the two calls answer
    differently, which is the whole reason a step's rating belongs to its step.
    """

    real = True

    def __init__(self, name: str, outputs: list[dict[str, Any]]) -> None:
        self.name = name
        self._outputs = list(outputs)

    async def run(self, intent: str, rationale: str, context: dict | None = None) -> Any:
        return self._outputs.pop(0)


def _plan(plan_id: str, *agent_ids: str) -> StoredPlan:
    return StoredPlan(
        id=plan_id,
        intent=INTENT,
        plan=Plan(
            steps=[
                PlanStep(
                    agent_id=agent_id,
                    agent_name=f"catalog.{agent_id}",
                    rationale="r",
                    est_price_usdc=PRICE,
                    est_eta_seconds=1.0,
                )
                for agent_id in agent_ids
            ]
        ),
        total_usdc=PRICE * len(agent_ids),
        total_eta=float(len(agent_ids)),
    )


def _rates(monkeypatch, worker: _AnswersInTurn, job_id: bytes = JOB_ID) -> _FakeLedger:
    """Put the run on the on-chain path with the charge stubbed and the ledger
    faked. Everything between — `_run`'s bookkeeping, `_submit_ratings`, the id
    derivation — is the code under test."""
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_signing_key", "SFAKEKEY")

    async def _resolve(agent_id: str) -> _AnswersInTurn | None:
        return worker

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)

    async def _fake_settle(task_id, start, plan, *, payer, auth_id_hex, total_usdc):
        return ("chargehash", "sealhash", job_id)

    monkeypatch.setattr(execution_svc, "_settle_onchain", _fake_settle)

    ledger = _FakeLedger()
    monkeypatch.setattr(sc, "submit_rating_async", ledger.submit)
    return ledger


def _run_paid(plan: StoredPlan, task_id: str) -> None:
    state.add_task(Task(id=task_id, intent=INTENT, agents=len(plan.plan.steps), spent=0.0, status="running"))
    asyncio.run(execution_svc._run(plan, task_id, auth_id_hex=AUTH, payer=PAYER))


# ── the model: the guard, in the contract's own order ───────────────────


def test_the_fake_ledger_refuses_a_second_rating_for_one_pair_whatever_its_kind():
    """Pins the premise the rest of the file rests on. If this ever stops
    holding, the tests below stop testing anything."""
    ledger = _FakeLedger()
    assert asyncio.run(ledger.submit("agt_x", JOB_ID, 95, 1, PAYER, "auto"))["status"] == "SUCCESS"

    with pytest.raises(sc.ContractError) as refused:
        asyncio.run(ledger.submit("agt_x", JOB_ID, 10, 1, PAYER, "dispute"))

    assert refused.value.code == LEDGER_REPLAY
    assert ledger.refused == [("agt_x", JOB_ID)]


# ── one agent, two steps, two ratings ───────────────────────────────────


def test_one_agent_on_two_steps_lands_two_ratings(monkeypatch):
    """Under the run's single job id the second submit was refused as a replay
    of the first, so the plan's second step left no evidence at all."""
    ledger = _rates(monkeypatch, _AnswersInTurn("external.agt_twice", [CLEAN, FLAWED]))
    task_id = "tsk_ratingid_two"

    _run_paid(_plan("pln_ratingid_two", "agt_twice", "agt_twice"), task_id)

    assert ledger.refused == []
    assert len(ledger.rated) == 2
    assert {agent for agent, _job in ledger.rated} == {"agt_twice"}


def test_each_of_one_agent_s_two_steps_is_rated_on_its_own_output(monkeypatch):
    """`delivered` was keyed by agent_id, so the second step's output overwrote
    the first's and both steps were graded on whichever landed last. A draft
    that passed the critic and a critique that found two violations are not the
    same work and must not carry the same score."""
    ledger = _rates(monkeypatch, _AnswersInTurn("external.agt_twice", [CLEAN, FLAWED]))
    task_id = "tsk_ratingid_own"

    _run_paid(_plan("pln_ratingid_own", "agt_twice", "agt_twice"), task_id)

    by_id = {job: rating for (_agent, job), (rating, _kind) in ledger.rated.items()}
    assert by_id[execution_svc.settlement_job_id(JOB_ID, 0)] == CLEAN_RATING
    assert by_id[execution_svc.settlement_job_id(JOB_ID, 1)] == FLAWED_RATING


def test_both_ratings_are_traced_as_proof_and_neither_as_a_failure(monkeypatch):
    """The buyer's own record of the run. The lost rating used to appear here
    as "reputation submit failed", which is how a collision looked to everyone
    who was not reading the contract."""
    _rates(monkeypatch, _AnswersInTurn("external.agt_twice", [CLEAN, FLAWED]))
    task_id = "tsk_ratingid_trace"

    _run_paid(_plan("pln_ratingid_trace", "agt_twice", "agt_twice"), task_id)

    lines = state.traces[task_id]
    assert len([ln for ln in lines if ln.level == "proof" and "reputation →" in ln.msg]) == 2
    assert not any("reputation submit failed" in ln.msg for ln in lines)


def test_a_repeated_agent_does_not_shift_the_other_steps(monkeypatch):
    """The derivation is per STEP INDEX, not per repeat, so a plan's ids do not
    move because an agent elsewhere in it happened to be hired twice."""
    ledger = _rates(monkeypatch, _AnswersInTurn("external.any", [CLEAN, FLAWED, CLEAN]))
    task_id = "tsk_ratingid_three"

    _run_paid(_plan("pln_ratingid_three", "agt_a", "agt_twice", "agt_twice"), task_id)

    assert ledger.refused == []
    assert sorted(job.hex() for _agent, job in ledger.rated) == sorted(
        execution_svc.settlement_job_id(JOB_ID, i).hex() for i in range(3)
    )


# ── a reviewer can still tie every rating to the job ────────────────────


def test_every_rating_id_carries_the_sealed_job_id_s_first_half(monkeypatch):
    """ADR 0009 D1's constraint, which this path now lives under too: SOW §6.1
    wants a reviewer who opens a rating on Stellar Expert to tie it back to the
    sealed job without insider knowledge. The first sixteen hex characters of
    every id below are the charge's and the attestation's own."""
    ledger = _rates(monkeypatch, _AnswersInTurn("external.agt_twice", [CLEAN, FLAWED]))
    task_id = "tsk_ratingid_link"

    _run_paid(_plan("pln_ratingid_link", "agt_twice", "agt_twice"), task_id)

    assert ledger.rated
    for _agent, job in ledger.rated:
        assert job[:8] == JOB_ID[:8]
        assert len(job) == 16


def test_step_zero_keeps_the_sealed_job_id_itself(monkeypatch):
    """Nothing already on the ledger changes meaning: the ids the settler has
    been writing all along are step-0 ids, and step 0 still writes under the
    charge's own job id."""
    ledger = _rates(monkeypatch, _AnswersInTurn("external.agt_twice", [CLEAN, FLAWED]))
    task_id = "tsk_ratingid_zero"

    _run_paid(_plan("pln_ratingid_zero", "agt_twice", "agt_twice"), task_id)

    assert ("agt_twice", JOB_ID) in ledger.rated


# ── the derivation itself ───────────────────────────────────────────────


def test_the_derived_id_is_the_documented_formula():
    """Recomputed from the formula in the docstring rather than from the
    function, so a change to the derivation fails here. It is not an ordinary
    test: the replay guard remembers every key it has ever seen, so changing
    the formula after a rating has landed makes a retry derive a NEW id the
    guard does not recognise — and rates the agent twice for one step."""
    expected = JOB_ID[:8] + hashlib.sha256(JOB_ID + b"orizon-settlement:v1" + (1).to_bytes(2, "big")).digest()[:8]
    assert execution_svc.settlement_job_id(JOB_ID, 1) == expected
    assert execution_svc.settlement_job_id(JOB_ID, 0) == JOB_ID


def test_a_step_s_automatic_and_dispute_ids_can_never_be_the_same_key():
    """Different tags, so the settler's rating of a step and the rating an
    upheld dispute of that same step writes land on two keys — which is the
    only reason the dispute rating can be written at all (ADR 0009 D1)."""
    from app.services.dispute_rating import dispute_job_id

    for step_index in range(4):
        automatic = execution_svc.settlement_job_id(JOB_ID, step_index)
        assert automatic != dispute_job_id(JOB_ID, step_index)


@pytest.mark.parametrize(
    ("job_id", "step_index"),
    [(b"\x01" * 15, 1), (b"", 1), (JOB_ID, -1), (JOB_ID, 65536)],
    ids=["short-job-id", "empty-job-id", "negative-step", "step-past-two-bytes"],
)
def test_an_id_that_cannot_be_derived_is_refused_not_truncated(job_id, step_index):
    """A truncated step index would land on a NEIGHBOUR's id, and a job id that
    is not the ledger's sixteen bytes is not a job id. Both raise, and the
    caller rates the run's other steps."""
    with pytest.raises(ValueError):
        execution_svc.settlement_job_id(job_id, step_index)


def test_a_derived_id_equal_to_the_job_id_is_refused_loudly(monkeypatch):
    """Unreachable by chance (2**-64), so forced: a digest whose first eight
    bytes reproduce the job id's own tail. Returning it would put the step's
    rating on step 0's key, where the ledger refuses it as a replay for ever —
    and a rating that can never be written names nothing, while a refusal to
    derive names the job and the step."""

    class _Echo:
        def __init__(self, data: bytes) -> None:
            self._tail = data[8:16]

        def digest(self) -> bytes:
            return self._tail + bytes(24)

    monkeypatch.setattr(hashlib, "sha256", _Echo)
    with pytest.raises(ValueError, match="equals the job id itself"):
        execution_svc.settlement_job_id(JOB_ID, 1)


def test_a_step_with_no_derivable_id_is_skipped_and_the_rest_are_rated(monkeypatch, caplog):
    """Best-effort, like every other failure in this loop: the step that cannot
    be rated is named in the log and in the buyer's trace, and the run's other
    steps still leave their evidence. The reason is NOT `failure_reason`'s "rpc
    error" — nothing was submitted, and the buyer's trace should not say one
    thing happened when another did."""
    ledger = _rates(monkeypatch, _AnswersInTurn("external.agt_twice", [CLEAN, FLAWED]))

    def _refuses(job_id: bytes, step_index: int) -> bytes:
        if step_index == 1:
            raise ValueError("forced")
        return job_id

    monkeypatch.setattr(execution_svc, "settlement_job_id", _refuses)
    task_id = "tsk_ratingid_skipped"

    with caplog.at_level(logging.ERROR, logger="app.services.execution_svc"):
        _run_paid(_plan("pln_ratingid_skipped", "agt_twice", "agt_twice"), task_id)

    assert list(ledger.rated) == [("agt_twice", JOB_ID)]
    assert state.tasks[task_id].status == "complete"
    logged = [r.getMessage() for r in caplog.records if r.name == "app.services.execution_svc"]
    assert any("no rating id" in m and "step 1" in m and JOB_ID.hex() in m and PAYER in m for m in logged), logged
    traced = [ln.msg for ln in state.traces[task_id] if ln.level == "error"]
    assert any("reputation submit skipped" in m and "no rating id" in m for m in traced), traced
    assert not any("rpc error" in m for m in traced)


def test_a_step_we_never_dispatched_does_not_cost_the_agent_s_other_step(monkeypatch):
    """`resolve_worker` fails OPEN and its read failures are negative-cached,
    so one blip can leave an agent unresolved at one step of a plan and
    resolvable at the next. `undispatched` was keyed by agent_id, so that one
    blip dropped BOTH of the agent's steps — including the one that delivered
    and was billed for. "Was never asked" is a fact about a STEP.
    """
    worker = _AnswersInTurn("external.agt_twice", [CLEAN])
    ledger = _rates(monkeypatch, worker)

    seen: list[str] = []

    async def _blips(agent_id: str) -> _AnswersInTurn | None:
        seen.append(agent_id)
        return None if len(seen) == 1 else worker

    monkeypatch.setattr(execution_svc, "resolve_worker", _blips)
    task_id = "tsk_ratingid_ghost"

    _run_paid(_plan("pln_ratingid_ghost", "agt_twice", "agt_twice"), task_id)

    # Step 1 delivered, so it is rated on its own output under its own id.
    step_one = execution_svc.settlement_job_id(JOB_ID, 1)
    assert list(ledger.rated) == [("agt_twice", step_one)]
    assert ledger.rated[("agt_twice", step_one)] == (CLEAN_RATING, "auto")
    # Step 0 was never asked, so nobody rates it — not even the 20/100 a step
    # that delivered nothing earns. Our outage is not their reputation
    # (ADR 0005 D5), and the run still says out loud that it happened.
    assert ("agt_twice", JOB_ID) not in ledger.rated
    assert any("unknown agent" in ln.msg for ln in state.traces[task_id])
