"""Story 5.01 AC4 — a restart mid-window does not lose the dispute.

    Given a settled workflow inside its dispute window, when the backend
    restarts and the buyer then disputes, the dispute is accepted and
    processed normally.

`tests/test_dispute_durability.py` restarts the STORE: rows written by one
store object are read back by another. This file restarts the WHOLE BACKEND,
over HTTP, the way Render does it. The first process runs a paid v2 workflow
through the real `/api/orchestrator/execute`, the real run and the real
settlement write, with the chain faked at the client seam (`test_settle_v2`'s
`_Chain`). Then the process is thrown away: its lifespan shuts down, and
everything it held in memory goes — the task, its trace, its read token, the
challenge table, the read-grant key, the store singletons and their pools.
Only the database is kept, a real Postgres (conftest `pg_dsn`). A second
process boots from scratch on it, and the buyer disputes there, with nothing
but their wallet.

What each test pins is what the buyer needs AFTER the restart, read from what
survived it:

  * the receipt: `GET /api/tasks/{id}/disputes` serves the settlement, the job
    id and the window from Postgres, while the task itself is gone;
  * the dispute: the challenge and the open are judged against the settlement
    record — its payer, its stamped window, its steps — and never against the
    task state or the task token, which the restart took;
  * the outcome: the uphold credits the payer and writes the dispute rating;
  * the privacy rule: the buyer's reason stays behind a proof, and the token
    from before the restart is no longer one; the payer's read grant is.

Under TASK_AUTH_REQUIRED the listing is gated, and the restart took the token
it admits. `test_under_enforcement_the_payer_reaches_the_listing_by_signature`
pins the way back in: a read grant the payer earns from the durable
settlement, before any dispute exists.

The in-memory store cannot keep this promise, and the last test says so, with
the boot line that tells an operator.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pg_support import events, settlements
from stellar_sdk import Keypair
from test_settle_v2 import SETTLE_TX, _auth, _Chain, _install, _Ok, _plan, _receipt

from app import task_auth
from app.config import settings
from app.main import app
from app.routers import disputes as disputes_router
from app.security import KeyedRateLimiter
from app.services import (
    authorization_guard,
    binding_store,
    dispute_store,
    dispute_svc,
    execution_svc,
    refund_svc,
    registry_sync,
)
from app.services import external_binding as eb
from app.state import state
from app.stellar import client as sc

BUYER = Keypair.random()
STRANGER = Keypair.random()
AUTH_ID_HEX = "ab" * 16
PLAN_ID = "pln_v2"
PRICES = (0.01, 0.02)
REASON = "step 1 returned a summary of the wrong document"
API_KEY = "operator-restart-key"
REFUND_TX = "cr" * 32
RATING_TX = "ra" * 32


class Settler:
    """The platform's credit transfer, counted: one uphold, one transfer."""

    def __init__(self) -> None:
        self.transfers: list[tuple[str, float, str | None]] = []

    async def __call__(self, buyer: str, amount_usdc: float, *, dispute_id: str | None = None) -> dict[str, Any]:
        self.transfers.append((buyer, amount_usdc, dispute_id))
        return {"status": "SUCCESS", "hash": REFUND_TX, "ledger": 90}


class Ledger:
    """The ReputationLedger's submit, counted: the dispute rating an uphold writes."""

    def __init__(self) -> None:
        self.submits: list[dict[str, Any]] = []

    async def __call__(
        self, agent_id: str, job_id: bytes, rating: int, weight: int, payer: str, kind: str
    ) -> dict[str, Any]:
        self.submits.append({"agent_id": agent_id, "job_id": job_id, "payer": payer, "kind": kind})
        return {"status": "SUCCESS", "hash": RATING_TX}


@pytest.fixture()
def deployment(monkeypatch: pytest.MonkeyPatch, pg_dsn: str) -> dict[str, Any]:
    """Production's shape, faked at the edges: Postgres, escrow v2, refunds and ratings on.

    Everything here is CONFIGURATION or the CHAIN, which a restart does not
    touch — so it is set once, for both processes. What a restart does touch is
    `_process`'s to throw away.
    """
    monkeypatch.setattr(settings, "database_url", pg_dsn)
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    monkeypatch.setattr(settings, "reputation_enabled", True)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", "CFAKELEDGER")
    monkeypatch.setattr(settings, "stellar_asset_sac", "CSAC" + "7Z2Q" * 12)
    monkeypatch.setattr(settings, "api_key", API_KEY)
    chain = _install(monkeypatch, _Chain(auth=_auth(payer=BUYER.public_key)))
    # The registry id `_install` sets is for the settle's `owner_of` reads; the
    # boot-time sync loop would read the fake chain for a list it never answers.
    monkeypatch.setattr(registry_sync, "start", lambda: None)

    async def _no_first_pass(_timeout: float) -> bool:
        return True

    monkeypatch.setattr(registry_sync, "wait_first_pass", _no_first_pass)
    monkeypatch.setattr(execution_svc, "_execute_refusal", lambda *a, **k: None)

    async def _resolve(agent_id: str) -> _Ok:
        return _Ok()

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)

    async def _no_settler_ratings(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(execution_svc, "_submit_ratings", _no_settler_ratings)
    settler = Settler()
    monkeypatch.setattr(refund_svc, "execute_refund", settler)
    ledger = Ledger()
    monkeypatch.setattr(sc, "submit_rating_async", ledger)
    return {"chain": chain, "settler": settler, "ledger": ledger, "dsn": pg_dsn}


@pytest.fixture(autouse=True)
def _the_suites_process_comes_back() -> Iterator[None]:
    """These tests wipe the one `state` the whole suite shares; put it back after.

    Shallow copies of each container, so a later test sees the agents, tasks
    and plans it would have seen had this file never run.
    """
    saved = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in vars(state).items()}
    challenges = eb._challenges.copy()
    yield
    vars(state).update(saved)
    eb._challenges.clear()
    eb._challenges.update(challenges)
    eb._exhausted.clear()
    dispute_store._store = None
    binding_store._store = None


def _forget_everything_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a process start gives the service: nothing it did not read from outside.

    `state` is reset IN PLACE because every module imported the one object.
    The read-grant key is re-drawn, as a new process draws it
    (`task_auth._READ_GRANT_KEY`), so a grant from before the restart is as
    dead here as it is on Render.
    """
    state.__init__()  # type: ignore[misc]
    eb._challenges.clear()
    eb._exhausted.clear()
    authorization_guard.forget_claims()
    dispute_store._store = None
    binding_store._store = None
    monkeypatch.setattr(task_auth, "_READ_GRANT_KEY", secrets.token_bytes(32))
    monkeypatch.setattr(
        disputes_router,
        "_challenge_limiter",
        KeyedRateLimiter(lambda: settings.dispute_challenge_rate_limit_per_minute),
    )


@contextmanager
def _process(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """One backend process, boot to shutdown, over whatever database is configured."""
    _forget_everything_in_memory(monkeypatch)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _forget_everything_in_memory(monkeypatch)


def _wait_for(read: Any, done: Any, what: str, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        answer = read()
        if done(answer):
            return answer
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}: last answer {answer!r}")
        time.sleep(0.02)


def _settle_a_paid_run(client: TestClient) -> tuple[str, str, str]:
    """Run the plan through execute, the run and the v2 settle; (task_id, read_token, job_id_hex)."""
    plan = _plan(PRICES)
    state.plans[plan.id] = plan
    r = client.post(
        "/api/orchestrator/execute",
        json={"plan_id": PLAN_ID, "auth_id_hex": AUTH_ID_HEX, "payer": BUYER.public_key},
    )
    assert r.status_code == 200, r.text
    task_id, token = r.json()["task_id"], r.json()["read_token"]
    listing = _wait_for(
        lambda: client.get(f"/api/tasks/{task_id}/disputes", headers={"X-Task-Token": token}).json(),
        lambda body: body["settlement"] is not None and body["settlement"]["proof_tx"] is not None,
        "the settlement and its seal to be recorded",
    )
    _wait_for(
        lambda: client.get(f"/api/tasks/{task_id}", headers={"X-Task-Token": token}).json(),
        lambda body: body["status"] == "complete",
        "the run to finish",
    )
    return task_id, token, listing["settlement"]["job_id_hex"]


def _before_the_restart(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    with _process(monkeypatch) as client:
        return _settle_a_paid_run(client)


def _sign(keypair: Keypair, message: str) -> str:
    """SEP-53, as Freighter signs through StellarWalletsKit's `signMessage`."""
    return base64.b64encode(keypair.sign_message(message.encode("utf-8"))).decode("ascii")


def _dispute(client: TestClient, job_id_hex: str, step_index: int, signer: Keypair, payer: str) -> Any:
    challenge = client.post("/api/disputes/challenge", json={"job_id_hex": job_id_hex, "step_index": step_index})
    assert challenge.status_code == 200, challenge.text
    body = challenge.json()
    return client.post(
        "/api/disputes",
        json={
            "job_id_hex": job_id_hex,
            "step_index": step_index,
            "reason": REASON,
            "payer": payer,
            "nonce": body["nonce"],
            "signature_b64": _sign(signer, body["message"]),
        },
    )


def _read_grant(client: TestClient, task_id: str, signer: Keypair = BUYER) -> Any:
    challenge = client.post("/api/disputes/read-challenge", json={"task_id": task_id})
    if challenge.status_code != 200:
        return challenge
    body = challenge.json()
    return client.post(
        "/api/disputes/read-grant",
        json={"task_id": task_id, "nonce": body["nonce"], "signature_b64": _sign(signer, body["message"])},
    )


def _rows(dsn: str) -> list[dict[str, Any]]:
    return asyncio.run(events(dsn))


# ── the acceptance criterion ────────────────────────────────────


def test_a_dispute_raised_after_a_restart_is_accepted_upheld_and_credited(
    monkeypatch: pytest.MonkeyPatch, deployment: dict[str, Any]
) -> None:
    task_id, token, _job = _before_the_restart(monkeypatch)
    recorded = asyncio.run(settlements(deployment["dsn"]))[-1]  # newest row: the one with the seal
    assert recorded["task_id"] == task_id and recorded["payer"] == BUYER.public_key

    with _process(monkeypatch) as client:
        # The restart took the task: this process never ran it.
        assert client.get(f"/api/tasks/{task_id}", headers={"X-Task-Token": token}).status_code == 404
        assert task_id not in state.tasks and task_id not in state.task_tokens

        # The receipt is read from what survived: the settlement in Postgres.
        listing = client.get(f"/api/tasks/{task_id}/disputes").json()
        settled = listing["settlement"]
        assert settled["payer"] == BUYER.public_key
        assert settled["charge_tx"] == SETTLE_TX
        assert [s["receipt_id_hex"] for s in settled["steps"]] == [_receipt(0).hex(), _receipt(1).hex()]
        assert listing["now"] < listing["window_closes_at"]
        assert listing["settlement_state"] == "settled"

        # The buyer disputes with nothing but their wallet.
        opened = _dispute(client, settled["job_id_hex"], 1, BUYER, BUYER.public_key)
        assert opened.status_code == 200, opened.text
        dispute = opened.json()
        assert (dispute["status"], dispute["task_id"], dispute["payer"]) == ("open", task_id, BUYER.public_key)
        assert dispute["charged_usdc"] == pytest.approx(0.02)
        assert dispute["reason"] == REASON

        upheld = client.post(f"/api/disputes/{dispute['id']}/uphold", headers={"X-API-Key": API_KEY})
        assert upheld.status_code == 200, upheld.text
        final = upheld.json()

    # Credited to the payer the settlement names, and rated under the step's derived id.
    assert (final["status"], final["refund_tx"], final["rating_tx"]) == ("credited", REFUND_TX, RATING_TX)
    assert final["rating_confirmed"] is True
    assert deployment["settler"].transfers == [(BUYER.public_key, pytest.approx(0.02), dispute["id"])]
    [rating] = deployment["ledger"].submits
    assert (rating["agent_id"], rating["payer"], rating["kind"]) == ("agt_1", BUYER.public_key, "dispute")
    # And it is all in Postgres, where the next restart will find it.
    assert [row["status"] for row in _rows(deployment["dsn"])][-1] == "credited"


def test_the_payer_reads_their_receipt_after_the_restart_and_a_stranger_does_not(
    monkeypatch: pytest.MonkeyPatch, deployment: dict[str, Any]
) -> None:
    """The reason stays behind a proof. The token from before the restart is not
    one any more; the payer's signature, checked against the durable settlement,
    is."""
    task_id, token, _job = _before_the_restart(monkeypatch)
    with _process(monkeypatch) as client:
        job = client.get(f"/api/tasks/{task_id}/disputes").json()["settlement"]["job_id_hex"]
        dispute_id = _dispute(client, job, 0, BUYER, BUYER.public_key).json()["id"]

    with _process(monkeypatch) as client:
        anonymous = client.get(f"/api/tasks/{task_id}/disputes").json()
        stale_token = client.get(f"/api/tasks/{task_id}/disputes", headers={"X-Task-Token": token}).json()
        stranger = _read_grant(client, task_id, STRANGER)
        granted = _read_grant(client, task_id)
        assert granted.status_code == 200, granted.text
        grant = {"X-Dispute-Read-Grant": granted.json()["grant"]}
        listing = client.get(f"/api/tasks/{task_id}/disputes", headers=grant).json()
        one = client.get(f"/api/disputes/{dispute_id}", headers=grant).json()
        one_anonymous = client.get(f"/api/disputes/{dispute_id}").json()

    for withheld in (anonymous["disputes"][0], stale_token["disputes"][0], one_anonymous):
        assert (withheld["reason"], withheld["reason_withheld"], withheld["status"]) == ("", True, "open")
    assert (stranger.status_code, stranger.json()["error"]["code"]) == (403, "not_the_payer")
    assert (listing["disputes"][0]["reason"], listing["disputes"][0]["reason_withheld"]) == (REASON, False)
    assert (one["reason"], one["reason_withheld"]) == (REASON, False)


# ── what the restart must not loosen ────────────────────────────


def test_after_a_restart_only_the_payer_may_dispute(
    monkeypatch: pytest.MonkeyPatch, deployment: dict[str, Any]
) -> None:
    """The payer is the SETTLEMENT's, read from Postgres: a stranger claiming
    to be them, or signing as themselves, is refused, and nothing is written."""
    task_id, _token, _job = _before_the_restart(monkeypatch)
    with _process(monkeypatch) as client:
        job = client.get(f"/api/tasks/{task_id}/disputes").json()["settlement"]["job_id_hex"]
        posing = _dispute(client, job, 0, STRANGER, BUYER.public_key)
        themselves = _dispute(client, job, 0, STRANGER, STRANGER.public_key)

    for refused in (posing, themselves):
        assert (refused.status_code, refused.json()["error"]["code"]) == (403, "not_the_payer")
    assert _rows(deployment["dsn"]) == []


def test_after_a_restart_the_stamped_window_still_closes(
    monkeypatch: pytest.MonkeyPatch, deployment: dict[str, Any]
) -> None:
    """The window is the one stamped on the settlement at settle time. A new
    process whose clock is past it refuses the dispute, at the mint and at the
    open, however recently it booted."""
    task_id, _token, _job = _before_the_restart(monkeypatch)
    recorded = asyncio.run(settlements(deployment["dsn"]))[-1]  # newest row: the one with the seal

    class _PastTheWindow:
        @staticmethod
        def time() -> float:
            return float(recorded["window_closes_at"]) + 1.0

    with _process(monkeypatch) as client:
        job = client.get(f"/api/tasks/{task_id}/disputes").json()["settlement"]["job_id_hex"]
        # A live challenge minted inside the window, so the OPEN is what is judged.
        challenge = client.post("/api/disputes/challenge", json={"job_id_hex": job, "step_index": 0}).json()
        monkeypatch.setattr(dispute_svc, "time", _PastTheWindow)
        mint = client.post("/api/disputes/challenge", json={"job_id_hex": job, "step_index": 1})
        opened = client.post(
            "/api/disputes",
            json={
                "job_id_hex": job,
                "step_index": 0,
                "reason": REASON,
                "payer": BUYER.public_key,
                "nonce": challenge["nonce"],
                "signature_b64": _sign(BUYER, challenge["message"]),
            },
        )

    for refused in (mint, opened):
        assert (refused.status_code, refused.json()["error"]["code"]) == (409, "dispute_window_closed")
    assert _rows(deployment["dsn"]) == []


# ── TASK_AUTH_REQUIRED: the token died with the process ─────────


def test_under_enforcement_the_payer_reaches_the_listing_by_signature(
    monkeypatch: pytest.MonkeyPatch, deployment: dict[str, Any]
) -> None:
    """The listing is where the console finds the job id for a FIRST dispute,
    and under enforcement it admits a task token or a read grant. The restart
    took the token, and no dispute exists yet — so the payer earns a grant from
    the durable settlement and goes on to dispute."""
    monkeypatch.setattr(settings, "task_auth_required", True)
    task_id, token, _job = _before_the_restart(monkeypatch)
    with _process(monkeypatch) as client:
        anonymous = client.get(f"/api/tasks/{task_id}/disputes")
        stale_token = client.get(f"/api/tasks/{task_id}/disputes", headers={"X-Task-Token": token})
        stranger = _read_grant(client, task_id, STRANGER)
        granted = _read_grant(client, task_id)
        assert granted.status_code == 200, granted.text
        listing = client.get(
            f"/api/tasks/{task_id}/disputes", headers={"X-Dispute-Read-Grant": granted.json()["grant"]}
        )
        assert listing.status_code == 200, listing.text
        opened = _dispute(client, listing.json()["settlement"]["job_id_hex"], 1, BUYER, BUYER.public_key)

    for refused in (anonymous, stale_token):
        assert (refused.status_code, refused.json()["error"]["code"]) == (404, "unknown_task")
    assert (stranger.status_code, stranger.json()["error"]["code"]) == (403, "not_the_payer")
    assert opened.status_code == 200, opened.text
    assert opened.json()["status"] == "open"


def test_under_enforcement_a_closed_undisputed_task_still_mints_no_read_challenge(
    monkeypatch: pytest.MonkeyPatch, deployment: dict[str, Any]
) -> None:
    """The first-dispute read is for a task that can still be disputed. Once the
    window closes with nothing raised there is nothing to read and nothing to
    start, and the `dispute_read` budget is not spent on it."""
    monkeypatch.setattr(settings, "task_auth_required", True)
    task_id, _token, _job = _before_the_restart(monkeypatch)
    recorded = asyncio.run(settlements(deployment["dsn"]))[-1]  # newest row: the one with the seal

    class _PastTheWindow:
        @staticmethod
        def time() -> float:
            return float(recorded["window_closes_at"]) + 1.0

    with _process(monkeypatch) as client:
        monkeypatch.setattr(dispute_svc, "time", _PastTheWindow)
        r = client.post("/api/disputes/read-challenge", json={"task_id": task_id})

    assert (r.status_code, r.json()["error"]["code"]) == (404, "no_disputes")
    assert eb._challenges == {}


# ── the limit: without DATABASE_URL a restart loses it ──────────


def test_without_a_database_the_restart_loses_the_settlement_and_the_boot_log_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, deployment: dict[str, Any]
) -> None:
    """The in-memory store is for local development and the test suite. It holds
    the settlement for as long as the process lives, and not a moment longer —
    and every boot on it says so at WARNING, so the operator learns it from the
    log rather than from a buyer."""
    monkeypatch.setattr(settings, "database_url", "")
    with caplog.at_level(logging.WARNING, logger="app.services.dispute_store"):
        task_id, _token, job = _before_the_restart(monkeypatch)
        with _process(monkeypatch) as client:
            listing = client.get(f"/api/tasks/{task_id}/disputes").json()
            mint = client.post("/api/disputes/challenge", json={"job_id_hex": job, "step_index": 0})

    assert (listing["settlement"], listing["window_closes_at"], listing["disputes"]) == (None, None, [])
    assert (mint.status_code, mint.json()["error"]["code"]) == (404, "unknown_job")
    boots = [r.getMessage() for r in caplog.records if r.getMessage().startswith("dispute store: in-memory")]
    assert len(boots) == 2, "each process must say at boot that its dispute records die with it"
    assert all("LOST on restart" in line and "DATABASE_URL" in line for line in boots)
