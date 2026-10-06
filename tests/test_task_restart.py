"""D-090 — a backend restart keeps the task, its receipt and its plan.

`tests/test_dispute_restart.py` restarts the whole backend over one real
Postgres to prove the SETTLEMENT survives. Until D-090 the task did not: the
receipt link a buyer was handed (`/api/tasks/{id}`) answered 404 `unknown_task`
after every Render restart, for a run that had been paid for. This file runs
the same restart — a first process boots, works and shuts down; everything it
held in memory is thrown away; a second boots on the same database — and pins
what the buyer can still read on the far side:

  * the task, exactly as it was: status, spend, settlement, both hashes;
  * its trace, line for line, and its artifact;
  * its read token, which still admits the holder and still refuses everyone
    else, under TASK_AUTH_REQUIRED — the store only ever held its digest;
  * a plan built before the restart, which still executes after it.

The processes, the chain fake and the database are `test_dispute_restart`'s,
reused rather than copied so the two restart suites cannot drift apart.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pg_support import fetch
from test_dispute_restart import (  # noqa: F401 — fixtures, requested by name
    _process,
    _settle_a_paid_run,
    _the_suites_process_comes_back,
    _wait_for,
    deployment,
)
from test_settle_v2 import SEAL_TX, SETTLE_TX

from app.config import settings
from app.services import orchestrator_svc, task_persistence
from app.state import state


def _receipt(client: TestClient, task_id: str, token: str) -> dict[str, Any]:
    """Everything the buyer's receipt page reads for a task."""
    headers = {"X-Task-Token": token}
    return {
        "task": client.get(f"/api/tasks/{task_id}", headers=headers).json(),
        "trace": client.get(f"/api/trace/{task_id}", headers=headers).json(),
        "artifact": client.get(f"/api/tasks/{task_id}/artifact", headers=headers).json(),
        "settlement": client.get(f"/api/tasks/{task_id}/disputes", headers=headers).json()["settlement"],
    }


def _other_token(token: str) -> str:
    """`token` with its last character flipped — never the token itself.

    `token[:-1] + "x"` WAS the token whenever it already ended in "x"
    (one urlsafe token in 64), which made the refusal assertion flaky.
    """
    return token[:-1] + ("B" if token.endswith("A") else "A")


def test_the_other_token_is_never_the_token() -> None:
    for token in ("abcx", "abcA", "abcB", "x", "A"):
        other = _other_token(token)
        assert other != token and len(other) == len(token) and other[:-1] == token[:-1]


def _comparable(receipt: dict[str, Any]) -> dict[str, Any]:
    # `started` is "2m ago", derived at serialization time from the stored
    # `started_at`, so it moves with the clock rather than with the restart.
    return receipt | {"task": {k: v for k, v in receipt["task"].items() if k != "started"}}


def test_a_paid_task_and_its_receipt_survive_a_restart(
    monkeypatch: pytest.MonkeyPatch,
    deployment: dict[str, Any],  # noqa: F811 — the fixture imported above
) -> None:
    monkeypatch.setattr(settings, "task_auth_required", True)
    with _process(monkeypatch) as client:
        task_id, token, _job = _settle_a_paid_run(client)
        before = _receipt(client, task_id, token)
    assert task_id not in state.tasks  # the first process and its memory are gone

    with _process(monkeypatch) as client:
        after = _receipt(client, task_id, token)
        anonymous = client.get(f"/api/tasks/{task_id}")
        wrong = client.get(f"/api/tasks/{task_id}", headers={"X-Task-Token": _other_token(token)})
        stream = client.get(f"/api/trace/{task_id}/stream", params={"token": token})

    assert _comparable(after) == _comparable(before)
    task = after["task"]
    assert (task["status"], task["settlement"], task["charge_tx"], task["proof_tx"]) == (
        "complete",
        "settled",
        SETTLE_TX,
        SEAL_TX,
    )
    assert after["settlement"]["charge_tx"] == SETTLE_TX
    assert any(line["settlement"] == "settled" for line in after["trace"])
    # A finished run's stream replays what was recorded and ends.
    assert stream.status_code == 200 and "event: done" in stream.text
    # The token is still the only way in, and the store never held it.
    for refused in (anonymous, wrong):
        assert (refused.status_code, refused.json()["error"]["code"]) == (404, "unknown_task")
    [row] = asyncio.run(fetch(deployment["dsn"], "SELECT * FROM task_records WHERE task_id = $1", task_id))
    assert token not in str(row) and row["read_token_sha256"]


def test_a_plan_built_before_a_restart_executes_after_it(
    monkeypatch: pytest.MonkeyPatch,
    deployment: dict[str, Any],  # noqa: F811 — the fixture imported above
) -> None:
    """The buyer reads the plan card and signs; Render restarts in between.
    The plan is read back and runs, instead of answering 404."""

    async def _no_thinking(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(orchestrator_svc, "_kit_thinking", _no_thinking, raising=False)
    with _process(monkeypatch) as client:
        built = client.post("/api/orchestrator/decompose", json={"intent": "pomodoro timer app"})
        assert built.status_code == 200, built.text
        plan_id = built.json()["plan_id"]

    with _process(monkeypatch) as client:
        assert plan_id not in state.plans
        executed = client.post("/api/orchestrator/execute", json={"plan_id": plan_id})
        assert executed.status_code == 200, executed.text
        task_id, token = executed.json()["task_id"], executed.json()["read_token"]
        done = _wait_for(
            lambda: client.get(f"/api/tasks/{task_id}", headers={"X-Task-Token": token}).json(),
            lambda body: body["status"] in ("complete", "failed"),
            "the restored plan's run to finish",
        )

    assert done["status"] == "complete"
    assert done["intent"] == "pomodoro timer app"


def test_without_a_database_the_restart_still_loses_the_task(
    monkeypatch: pytest.MonkeyPatch,
    deployment: dict[str, Any],  # noqa: F811 — the fixture imported above
) -> None:
    """The in-memory default keeps nothing across a restart — the limit this
    whole change exists to lift, stated so a deployment without DATABASE_URL
    cannot be mistaken for one with it."""
    monkeypatch.setattr(settings, "database_url", "")
    with _process(monkeypatch) as client:
        task_id, token, _job = _settle_a_paid_run(client)
    with _process(monkeypatch) as client:
        gone = client.get(f"/api/tasks/{task_id}", headers={"X-Task-Token": token})
    assert gone.status_code == 404
    assert asyncio.run(task_persistence.flush(1.0)) is True
