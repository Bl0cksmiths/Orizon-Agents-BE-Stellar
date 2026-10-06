"""D-067: the payer reads their own disputes' free text by signature.

The gate that withholds a dispute's `reason` and `rejection_reason` from
strangers stays exactly as it was. What changes is what it accepts: besides the
task token (which dies with the process, the tab and the 200-task ring) and the
operator key (which no payer holds), a DISPUTE READ GRANT, earned by signing a
read challenge with the wallet the settlement names.

Everything here runs the real crypto and the real challenge table; only the
dispute store's reads are stubbed, at the seam the routes import. The tests are
written as the properties the grant must have rather than as the code paths it
happens to take:

  * the payer, and only the payer, gets one — a stranger's valid signature is
    `not_the_payer`, a replayed nonce is a 409;
  * with it, both read routes carry both reasons and `reason_withheld` is
    false; with anything else — nothing, an expired grant, another task's,
    another payer's, a tampered one — they carry neither and say so;
  * it buys the free text and NOTHING ELSE: no status, artifact, trace or
    stream, whatever TASK_AUTH_REQUIRED says;
  * reading never costs disputing: its challenges have their own budget and its
    proofs never spend a dispute nonce;
  * a signature for one of the two purposes is never a proof of the other;
  * none of the opening rules apply — a closed window still reads.
"""

from __future__ import annotations

import base64
import math
import time

import pytest
from stellar_sdk import Keypair

from app import task_auth
from app.config import settings
from app.schemas import Task
from app.services import dispute_svc
from app.services import external_binding as eb
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep
from app.state import state

PAYER = Keypair.random()
STRANGER = Keypair.random()

# A task id that is also a well-formed job id, on purpose. The cross-purpose
# tests below need the two flows to be ABLE to collide — same scope string,
# step 0 — so that what keeps them apart is the domain and the key space, not
# the accident of a task id and a job id never being equal.
SHARED_ID = "0123456789abcdef0123456789abcdef"
TASK = "tsk_d067read"
OTHER_TASK = "tsk_d067other"
REASON = "the step returned an empty file"
NOTE = "the delivered file matched the brief line for line"
DISPUTE_ID = "dsp_0011223344556677"
TOKEN = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"

READ_ROUTES = [f"/api/tasks/{TASK}/disputes", f"/api/disputes/{DISPUTE_ID}"]
READ_IDS = ["per-task", "one-dispute"]


def _sign(keypair: Keypair, message: str) -> str:
    return base64.b64encode(keypair.sign(message.encode("utf-8"))).decode("ascii")


def _sign_sep53(keypair: Keypair, message: str) -> str:
    """What Freighter produces through StellarWalletsKit's `signMessage`."""
    return base64.b64encode(keypair.sign_message(message.encode("utf-8"))).decode("ascii")


def settlement(task_id: str = TASK, **overrides: object) -> SettlementRecord:
    fields: dict = {
        "task_id": task_id,
        "payer": PAYER.public_key,
        "auth_id_hex": "fedcba0987654321fedcba0987654321",
        "job_id_hex": SHARED_ID,
        "charge_tx": "abc123",
        "proof_tx": "def456",
        "settled_usdc": 0.25,
        "steps": (
            SettlementStep(step_index=0, agent_id="code-agent", agent_name="Coder", price_usdc=0.25, delivered=True),
        ),
        "settled_at": time.time() - 60,
        "window_closes_at": time.time() + 86_400,
    }
    fields.update(overrides)
    return SettlementRecord(**fields)


def rejected(task_id: str = TASK) -> DisputeRecord:
    return DisputeRecord(
        id=DISPUTE_ID,
        job_id_hex=SHARED_ID,
        task_id=task_id,
        step_index=0,
        agent_id="code-agent",
        payer=PAYER.public_key,
        reason=REASON,
        status="rejected",
        charged_usdc=0.25,
        creditable_usdc=0.25,
        opened_at=1_700_000_000.0,
        resolved_at=1_700_000_500.0,
        note=NOTE,
    )


@pytest.fixture(autouse=True)
def empty_table():
    """A private challenge table per test, restored afterwards."""
    saved = eb._challenges.copy()
    eb._challenges.clear()
    eb._exhausted.clear()
    try:
        yield eb._challenges
    finally:
        eb._challenges.clear()
        eb._challenges.update(saved)
        eb._exhausted.clear()


@pytest.fixture()
def settled(monkeypatch):
    """`TASK` settled by PAYER, with one rejected dispute on it.

    Stubbed at `dispute_svc`, the seam both the read routes and the grant
    service call through. `OTHER_TASK` is settled by the same payer, so a grant
    for it is a real grant — only for the wrong task.
    """
    records = {TASK: settlement(TASK), OTHER_TASK: settlement(OTHER_TASK)}

    async def _settlement(task_id: str) -> SettlementRecord | None:
        return records.get(task_id)

    async def _list(task_id: str) -> tuple[DisputeRecord, ...]:
        return (rejected(task_id),) if task_id in records else ()

    async def _get(dispute_id: str) -> DisputeRecord | None:
        return rejected() if dispute_id == DISPUTE_ID else None

    monkeypatch.setattr(dispute_svc, "settlement_for_task", _settlement)
    monkeypatch.setattr(dispute_svc, "list_for_task", _list)
    monkeypatch.setattr(dispute_svc, "get_dispute", _get)
    monkeypatch.setattr(settings, "task_auth_required", False)
    monkeypatch.setattr(settings, "api_key", "")
    return records


def _challenge(client, task_id: str = TASK) -> dict:
    r = client.post("/api/disputes/read-challenge", json={"task_id": task_id})
    assert r.status_code == 200, r.text
    return r.json()


def _grant(client, task_id: str = TASK, signer: Keypair = PAYER, *, sep53: bool = False) -> dict:
    challenge = _challenge(client, task_id)
    sign = _sign_sep53 if sep53 else _sign
    r = client.post(
        "/api/disputes/read-grant",
        json={"task_id": task_id, "nonce": challenge["nonce"], "signature_b64": sign(signer, challenge["message"])},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _dispute_of(body: dict) -> dict:
    return body["disputes"][0] if "disputes" in body else body


def _read(client, path: str, grant: str | None) -> dict:
    headers = {} if grant is None else {"X-Dispute-Read-Grant": grant}
    r = client.get(path, headers=headers)
    assert r.status_code == 200, r.text
    return _dispute_of(r.json())


# ── the payer gets a grant, and the grant buys both reasons ─────


@pytest.mark.parametrize("sep53", [False, True], ids=["raw-bytes", "sep53"])
@pytest.mark.parametrize("path", READ_ROUTES, ids=READ_IDS)
def test_the_payer_signs_once_and_reads_both_reasons_on_both_routes(client, settled, path, sep53):
    """The whole of D-067, from a tab that never held the task token."""
    before = time.time()
    granted = _grant(client, sep53=sep53)

    assert granted["expires_at"] <= before + task_auth.READ_GRANT_TTL_SECONDS + 1
    assert granted["expires_at"] > time.time()
    dispute = _read(client, path, granted["grant"])
    assert dispute["reason"] == REASON
    assert dispute["rejection_reason"] == NOTE
    assert dispute["reason_withheld"] is False


def test_the_challenge_is_the_frozen_contract(client, settled):
    challenge = _challenge(client)

    assert set(challenge) == {"nonce", "message", "expires_at"}
    assert challenge["message"] == f"orizon-dispute-read:v1:{TASK}:{challenge['nonce']}"
    assert challenge["expires_at"] <= time.time() + eb.CHALLENGE_TTL_SECONDS


def test_the_grant_expires_within_the_hour(client, settled):
    before = time.time()
    granted = _grant(client)

    assert set(granted) == {"grant", "expires_at"}
    assert granted["expires_at"] <= before + 3600 + 1


# ── everything else gets neither reason, and is told so ─────────


def _expired_grant() -> str:
    grant, _ = task_auth.mint_read_grant(TASK, PAYER.public_key, now=time.time() - 2 * task_auth.READ_GRANT_TTL_SECONDS)
    return grant


def _tampered_grant() -> str:
    """A real grant for this task with its body rewritten to another task, MAC kept."""
    grant, _ = task_auth.mint_read_grant(OTHER_TASK, PAYER.public_key)
    version, _body, mac = grant.split(".")
    forged = base64.urlsafe_b64encode(f"{TASK}\n{PAYER.public_key}\n{int(time.time()) + 3000}".encode())
    return f"{version}.{forged.rstrip(b'=').decode()}.{mac}"


def _flipped_mac_grant() -> str:
    grant, _ = task_auth.mint_read_grant(TASK, PAYER.public_key)
    head, mac = grant.rsplit(".", 1)
    return f"{head}.{('B' if mac[0] == 'A' else 'A') + mac[1:]}"


WITHHELD = {
    "no-grant": lambda: None,
    "expired": _expired_grant,
    "another-tasks": lambda: task_auth.mint_read_grant(OTHER_TASK, PAYER.public_key)[0],
    "another-payers": lambda: task_auth.mint_read_grant(TASK, STRANGER.public_key)[0],
    "tampered-body": _tampered_grant,
    "tampered-mac": _flipped_mac_grant,
    "garbage": lambda: "g1.not.agrant",
    "oversized": lambda: "g1." + "A" * 600 + ".x",
}


@pytest.mark.parametrize("label", list(WITHHELD))
@pytest.mark.parametrize("path", READ_ROUTES, ids=READ_IDS)
def test_anything_but_a_live_grant_for_this_task_reads_nothing(client, settled, path, label):
    dispute = _read(client, path, WITHHELD[label]())

    assert dispute["reason"] == "", label
    assert dispute["rejection_reason"] is None, label
    assert dispute["reason_withheld"] is True, label
    # And the money facts are still all there — withholding is of the words.
    assert dispute["status"] == "rejected"


def test_a_grant_minted_by_another_process_reads_nothing(client, settled, monkeypatch):
    """A restart is a new key: the payer is asked to sign again, never shown
    text on the strength of a grant this process did not mint."""
    grant = _grant(client)["grant"]
    monkeypatch.setattr(task_auth, "_READ_GRANT_KEY", b"\x00" * 32)

    assert _read(client, READ_ROUTES[0], grant)["reason_withheld"] is True


# ── the grant buys the free text and nothing else ───────────────


@pytest.fixture()
def running_task():
    """`TASK` in memory with a read token, so the guarded routes have something to guard."""
    state.tasks[TASK] = Task(id=TASK, intent="read my reason", agents=1, spent=0.0, status="complete", started="now")
    state.traces[TASK] = []
    state.task_tokens[TASK] = TOKEN
    yield
    state.tasks.pop(TASK, None)
    state.traces.pop(TASK, None)
    state.task_tokens.pop(TASK, None)


GUARDED = [f"/api/tasks/{TASK}", f"/api/tasks/{TASK}/artifact", f"/api/trace/{TASK}", f"/api/trace/{TASK}/stream"]


@pytest.mark.parametrize("path", GUARDED, ids=["status", "artifact", "trace", "stream"])
def test_the_grant_never_opens_the_tasks_guarded_reads(client, settled, running_task, monkeypatch, path):
    """`require_task_read` never looks at the grant — in the header or anywhere
    else — so with enforcement on, a grant-holder is a stranger to the task."""
    grant = _grant(client)["grant"]
    monkeypatch.setattr(settings, "task_auth_required", True)

    r = client.get(path, headers={"X-Dispute-Read-Grant": grant})

    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "unknown_task"


@pytest.mark.parametrize("path", GUARDED[:3], ids=["status", "artifact", "trace"])
def test_the_token_still_opens_what_the_grant_does_not(client, settled, running_task, monkeypatch, path):
    """The control for the test above: the 404 is the grant being refused, not
    the route being unreachable."""
    monkeypatch.setattr(settings, "task_auth_required", True)

    assert client.get(path, headers={"X-Task-Token": TOKEN}).status_code == 200


def test_with_task_auth_on_the_grant_still_reaches_the_listing(client, settled, monkeypatch):
    """D-067 under TASK_AUTH_REQUIRED: the listing was gated by
    `require_task_read`, which never looks at a grant, so the payer holding one
    was answered 404 `unknown_task` by the one route their receipt reads."""
    grant = _grant(client)["grant"]
    monkeypatch.setattr(settings, "task_auth_required", True)

    r = client.get(READ_ROUTES[0], headers={"X-Dispute-Read-Grant": grant})

    assert r.status_code == 200, r.text
    assert _dispute_of(r.json())["reason"] == REASON
    assert _dispute_of(r.json())["reason_withheld"] is False


@pytest.mark.parametrize("label", list(WITHHELD))
def test_with_task_auth_on_nothing_but_a_live_grant_reaches_the_listing(client, settled, monkeypatch, label):
    """And nothing wider: every grant the free-text gate refuses is refused
    the listing too, as the same bare 404 a stranger gets."""
    monkeypatch.setattr(settings, "task_auth_required", True)
    grant = WITHHELD[label]()

    r = client.get(READ_ROUTES[0], headers={} if grant is None else {"X-Dispute-Read-Grant": grant})

    assert (r.status_code, r.json()["error"]["code"]) == (404, "unknown_task"), label


def test_with_task_auth_on_a_grant_for_a_task_with_no_settlement_is_refused(client, settled, monkeypatch):
    grant, _ = task_auth.mint_read_grant("tsk_unsettled", PAYER.public_key)
    monkeypatch.setattr(settings, "task_auth_required", True)

    r = client.get("/api/tasks/tsk_unsettled/disputes", headers={"X-Dispute-Read-Grant": grant})

    assert (r.status_code, r.json()["error"]["code"]) == (404, "unknown_task")


def test_with_task_auth_on_the_token_still_reaches_the_listing(client, settled, running_task, monkeypatch):
    monkeypatch.setattr(settings, "task_auth_required", True)

    r = client.get(READ_ROUTES[0], headers={"X-Task-Token": TOKEN})

    assert r.status_code == 200
    assert _dispute_of(r.json())["reason"] == REASON


def test_the_grant_is_not_a_task_read_proof():
    """At the seam itself: `proves` — the one answer every task-scoped guard
    shares — is no for a grant-holder; only `proves_free_text` is yes."""
    grant, _ = task_auth.mint_read_grant(TASK, PAYER.public_key)
    proof = task_auth.TaskReadProof(task_token=None, api_key=None, read_grant=grant)

    assert proof.proves(TASK) is False
    assert proof.proves_free_text(TASK, PAYER.public_key) is True


# ── only the payer, and only once per nonce ─────────────────────


def test_a_strangers_valid_signature_is_not_the_payer_and_spends_nothing(client, settled):
    challenge = _challenge(client)
    stranger = _sign(STRANGER, challenge["message"])

    r = client.post(
        "/api/disputes/read-grant", json={"task_id": TASK, "nonce": challenge["nonce"], "signature_b64": stranger}
    )

    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "not_the_payer"
    # The stranger's attempt did not cancel the payer's challenge.
    r = client.post(
        "/api/disputes/read-grant",
        json={"task_id": TASK, "nonce": challenge["nonce"], "signature_b64": _sign(PAYER, challenge["message"])},
    )
    assert r.status_code == 200, r.text


def test_a_malformed_signature_is_not_the_payer(client, settled):
    challenge = _challenge(client)

    r = client.post(
        "/api/disputes/read-grant", json={"task_id": TASK, "nonce": challenge["nonce"], "signature_b64": "@@@"}
    )

    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "not_the_payer"


def test_a_replayed_nonce_is_refused(client, settled):
    challenge = _challenge(client)
    body = {"task_id": TASK, "nonce": challenge["nonce"], "signature_b64": _sign(PAYER, challenge["message"])}
    assert client.post("/api/disputes/read-grant", json=body).status_code == 200

    r = client.post("/api/disputes/read-grant", json=body)

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "challenge_unknown"


def test_an_expired_challenge_is_refused_as_expired(client, settled):
    nonce, _ = eb.issue_dispute_read_challenge(TASK, ttl_seconds=-1)
    message = eb.dispute_read_message(TASK, nonce)

    r = client.post(
        "/api/disputes/read-grant", json={"task_id": TASK, "nonce": nonce, "signature_b64": _sign(PAYER, message)}
    )

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "challenge_expired"


def test_a_nonce_for_another_task_is_unknown_here(client, settled):
    other = _challenge(client, OTHER_TASK)

    r = client.post(
        "/api/disputes/read-grant",
        json={"task_id": TASK, "nonce": other["nonce"], "signature_b64": _sign(PAYER, other["message"])},
    )

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "challenge_unknown"


@pytest.mark.parametrize("route", ["read-challenge", "read-grant"])
def test_a_task_with_no_payer_to_prove_is_404(client, settled, route):
    body = {"task_id": "tsk_nobodyhome", "nonce": "0" * 32, "signature_b64": "AAAA"}
    state.tasks["tsk_unsettled"] = Task(
        id="tsk_unsettled", intent="x", agents=1, spent=0.0, status="running", started="now"
    )
    try:
        unknown = client.post(f"/api/disputes/{route}", json=body)
        unsettled = client.post(f"/api/disputes/{route}", json={**body, "task_id": "tsk_unsettled"})
    finally:
        state.tasks.pop("tsk_unsettled", None)

    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "unknown_task")
    assert (unsettled.status_code, unsettled.json()["error"]["code"]) == (404, "no_settlement")


def test_a_settled_task_with_nothing_disputed_mints_no_read_challenge(client, settled, empty_table, monkeypatch):
    """Settled alone used to be enough, so any hundred settled tasks held the
    whole `dispute_read` budget. A task nobody disputed has nothing to read."""
    settled["tsk_undisputed"] = settlement("tsk_undisputed")

    async def _list(task_id: str) -> tuple[DisputeRecord, ...]:
        return () if task_id == "tsk_undisputed" else (rejected(task_id),)

    monkeypatch.setattr(dispute_svc, "list_for_task", _list)

    r = client.post("/api/disputes/read-challenge", json={"task_id": "tsk_undisputed"})

    assert (r.status_code, r.json()["error"]["code"]) == (404, "no_disputes")
    assert len(empty_table) == 0


def test_a_hundred_undisputed_tasks_cannot_hold_the_read_budget(client, settled, monkeypatch):
    """The audit's scenario: a full budget's worth of settled, undisputed tasks
    is minted against, and the payer with a dispute still gets a challenge."""
    budget = eb.CHALLENGE_BUDGETS["dispute_read"]
    for i in range(budget):
        settled[f"tsk_any{i}"] = settlement(f"tsk_any{i}")

    async def _list(task_id: str) -> tuple[DisputeRecord, ...]:
        return (rejected(task_id),) if task_id == TASK else ()

    monkeypatch.setattr(dispute_svc, "list_for_task", _list)

    # From a different address each, as a budget-sized flood has to arrive:
    # one client is held to its per-route budget long before this (app/rate_limit.py).
    codes = {
        client.post(
            "/api/disputes/read-challenge",
            json={"task_id": f"tsk_any{i}"},
            headers={"x-forwarded-for": f"198.51.{i // 250}.{i % 250}"},
        ).status_code
        for i in range(budget)
    }

    assert codes == {404}
    assert _challenge(client)["nonce"]


@pytest.mark.parametrize(
    "body",
    [
        {"task_id": "tsk:colon"},
        {"task_id": "tsk\nnewline"},
        {"task_id": "t" * 129},
        {"task_id": TASK, "nonce": "0" * 129},
        {"task_id": TASK, "signature_b64": "A" * 257},
    ],
    ids=["colon", "newline", "long-task", "long-nonce", "long-signature"],
)
def test_the_new_routes_bound_what_they_accept(client, settled, body):
    full = {"task_id": TASK, "nonce": "0" * 32, "signature_b64": "AAAA", **body}
    route = "read-challenge" if set(body) == {"task_id"} else "read-grant"

    r = client.post(f"/api/disputes/{route}", json=full if route == "read-grant" else {"task_id": body["task_id"]})

    assert r.status_code == 422, r.text


# ── reading never costs disputing ───────────────────────────────


def test_filling_the_read_budget_leaves_the_dispute_mint_alone(empty_table):
    """Read challenges up to their budget, then one more is refused as
    `dispute_read` — and the payer can still mint a DISPUTE challenge."""
    minted = 0
    with pytest.raises(eb.ChallengeBudgetExhausted) as info:
        for i in range(eb.MAX_CHALLENGES * 2):
            eb.issue_dispute_read_challenge(f"tsk_flood_{i}")
            minted += 1

    assert info.value.purpose == "dispute_read"
    assert minted == eb.CHALLENGE_BUDGETS["dispute_read"]
    nonce, _ = eb.issue_dispute_challenge(SHARED_ID, 0)
    assert eb.dispute_challenge_is_live(SHARED_ID, 0, nonce) is True


def test_a_full_read_budget_is_a_503_with_its_own_code(client, settled):
    for i in range(eb.CHALLENGE_BUDGETS["dispute_read"]):
        eb.issue_dispute_read_challenge(f"tsk_flood_{i}")

    r = client.post("/api/disputes/read-challenge", json={"task_id": TASK})

    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "challenge_capacity_dispute_read"


def test_filling_the_dispute_budget_leaves_the_read_mint_alone(empty_table):
    for i in range(eb.CHALLENGE_BUDGETS["dispute"]):
        eb.issue_dispute_challenge(f"job_{i}", 0)

    nonce, _ = eb.issue_dispute_read_challenge(TASK)
    assert eb.dispute_read_challenge_state(TASK, nonce) == "live"


def test_a_read_never_consumes_a_dispute_nonce(empty_table):
    """Same scope string, step 0: the one arrangement in which the two flows
    could share a key. They do not, so proving a read leaves the dispute
    challenge live and signable."""
    dispute_nonce, _ = eb.issue_dispute_challenge(SHARED_ID, 0)
    read_nonce, _ = eb.issue_dispute_read_challenge(SHARED_ID)

    assert read_nonce != dispute_nonce
    assert eb.verify_dispute_read_challenge(
        SHARED_ID, PAYER.public_key, _sign(PAYER, eb.dispute_read_message(SHARED_ID, read_nonce))
    )
    assert eb.dispute_challenge_is_live(SHARED_ID, 0, dispute_nonce) is True
    assert eb.verify_dispute_challenge(
        SHARED_ID, 0, PAYER.public_key, _sign(PAYER, eb.dispute_message(SHARED_ID, 0, dispute_nonce))
    )


def test_a_dispute_never_consumes_a_read_nonce(empty_table):
    dispute_nonce, _ = eb.issue_dispute_challenge(SHARED_ID, 0)
    read_nonce, _ = eb.issue_dispute_read_challenge(SHARED_ID)

    assert eb.verify_dispute_challenge(
        SHARED_ID, 0, PAYER.public_key, _sign(PAYER, eb.dispute_message(SHARED_ID, 0, dispute_nonce))
    )
    assert eb.dispute_read_challenge_state(SHARED_ID, read_nonce) == "live"


# ── a signature for one purpose is never a proof of the other ───


def test_a_dispute_signature_is_not_a_read_proof(empty_table):
    """The payer's own signature over a DISPUTE message, on the read challenge's
    very nonce, with the scope and step arranged to collide if anything could."""
    read_nonce, _ = eb.issue_dispute_read_challenge(SHARED_ID)
    dispute_sig = _sign(PAYER, eb.dispute_message(SHARED_ID, 0, read_nonce))

    assert eb.verify_dispute_read_challenge(SHARED_ID, PAYER.public_key, dispute_sig) is False
    assert eb.dispute_read_challenge_state(SHARED_ID, read_nonce) == "live"


def test_a_dispute_signature_is_refused_by_the_grant_route(client, settled):
    # The task id standing where a dispute's job id goes, step 0: the dispute
    # message closest to the read message that the payer could be induced to sign.
    challenge = _challenge(client)
    dispute_sig = _sign(PAYER, eb.dispute_message(TASK, 0, challenge["nonce"]))

    r = client.post(
        "/api/disputes/read-grant", json={"task_id": TASK, "nonce": challenge["nonce"], "signature_b64": dispute_sig}
    )

    assert (r.status_code, r.json()["error"]["code"]) == (403, "not_the_payer"), r.text


def test_a_read_signature_is_not_a_dispute_proof(empty_table):
    dispute_nonce, _ = eb.issue_dispute_challenge(SHARED_ID, 0)
    read_sig = _sign(PAYER, eb.dispute_read_message(SHARED_ID, dispute_nonce))

    assert eb.verify_dispute_challenge(SHARED_ID, 0, PAYER.public_key, read_sig) is False
    assert eb.dispute_challenge_is_live(SHARED_ID, 0, dispute_nonce) is True


def test_the_four_domains_never_prefix_one_another():
    prefixes = [
        eb.BINDING_MESSAGE_PREFIX,
        eb.UNBINDING_MESSAGE_PREFIX,
        eb.DISPUTE_MESSAGE_PREFIX,
        eb.DISPUTE_READ_MESSAGE_PREFIX,
    ]
    for a in prefixes:
        for b in prefixes:
            if a != b:
                assert not (a + ":").startswith(b + ":"), (a, b)


def test_the_read_key_space_spends_the_read_budget():
    assert eb._purpose_of(eb.DISPUTE_READ_SUBJECT) == "dispute_read"
    assert eb._purpose_of(eb.dispute_subject(0)) == "dispute"
    assert eb.MAX_CHALLENGES == sum(eb.CHALLENGE_BUDGETS.values())


# ── none of the opening rules apply ─────────────────────────────


def test_a_closed_window_still_reads(client, settled):
    """Reading what was said is not making a new claim: a payer reads their
    rejection a week after the window closed, on a settlement with nothing
    left to dispute."""
    long_ago = time.time() - 7 * 86_400
    settled[TASK] = settlement(
        TASK,
        settled_at=long_ago,
        window_closes_at=long_ago + 86_400,
        steps=(SettlementStep(step_index=0, agent_id="a", agent_name=None, price_usdc=0.0, delivered=False),),
    )

    granted = _grant(client)

    assert _read(client, READ_ROUTES[0], granted["grant"])["reason"] == REASON


# ── the edges of the free-text gate, pinned one by one ──────────
#
# Each of these was a mutation the rest of the suite let through (the Epic 4
# API audit's survivors). They are the exact boundaries of "who reads the
# buyer's words", so each is pinned where it would otherwise move silently.


@pytest.mark.parametrize("path", READ_ROUTES, ids=READ_IDS)
def test_an_empty_key_on_a_keyless_deployment_buys_no_free_text(client, settled, path):
    """S03, the one that matters most. On a keyless deployment — the demo, and
    every deployment with refunds off — an EMPTY `X-API-Key` compared equal to
    the empty configured key under a one-clause mutation of
    `header_secret_matches`, and every buyer's reason went to anyone who sent
    the header blank."""
    assert settings.api_key == ""

    r = client.get(path, headers={"X-API-Key": ""})

    assert r.status_code == 200
    dispute = _dispute_of(r.json())
    assert (dispute["reason"], dispute["rejection_reason"], dispute["reason_withheld"]) == ("", None, True)


def test_an_empty_key_on_a_keyless_deployment_opens_no_guarded_read(client, settled, running_task, monkeypatch):
    # The same compare admits `require_task_read`: with enforcement on, an
    # empty header must not stand in for the operator key there either.
    monkeypatch.setattr(settings, "task_auth_required", True)

    assert client.get(f"/api/tasks/{TASK}", headers={"X-API-Key": ""}).status_code == 404


def test_a_grant_for_a_task_whose_settlement_is_gone_is_withheld_not_a_500(client, settled, monkeypatch):
    """T08: the grant check reads the settlement's payer, and with no
    settlement there is no payer. Without the `payer is None` guard that was an
    AttributeError — a 500 on the receipt's own read."""
    grant = _grant(client)["grant"]
    settled.pop(TASK)

    async def _still_listed(task_id: str) -> tuple[DisputeRecord, ...]:
        return (rejected(task_id),)

    monkeypatch.setattr(dispute_svc, "list_for_task", _still_listed)

    r = client.get(READ_ROUTES[0], headers={"X-Dispute-Read-Grant": grant})

    assert r.status_code == 200, r.text
    assert _dispute_of(r.json())["reason_withheld"] is True
    assert task_auth.read_grant_admits(grant, TASK, None) is False


@pytest.mark.parametrize("path", READ_ROUTES, ids=READ_IDS)
def test_the_task_token_in_the_query_buys_the_free_text(client, settled, running_task, path):
    """T11: EventSource cannot set headers, so the token also rides as
    `?token=`, and `task_read_proof` must read it there too — otherwise a
    client that only has the query form is told its own words are withheld."""
    dispute = _dispute_of(client.get(f"{path}?token={TOKEN}").json())

    assert dispute["reason"] == REASON
    assert dispute["reason_withheld"] is False


def test_a_non_ascii_read_nonce_is_unknown_not_a_500(client, settled):
    """E13: `compare_digest` raises TypeError on a str holding non-ASCII, so
    the read nonce is screened first, exactly as the dispute nonce is."""
    _challenge(client)

    r = client.post("/api/disputes/read-grant", json={"task_id": TASK, "nonce": "é" * 32, "signature_b64": "AAAA"})

    assert (r.status_code, r.json()["error"]["code"]) == (409, "challenge_unknown")


def test_a_refused_read_mint_takes_no_slot(client, settled, empty_table):
    """D03: the table is bounded and public, so a mint that is refused — no
    settlement, no payer to prove — must be refused BEFORE a slot is taken,
    not after."""
    r = client.post("/api/disputes/read-challenge", json={"task_id": "tsk_nobody"})

    assert (r.status_code, r.json()["error"]["code"]) == (404, "unknown_task")
    assert len(empty_table) == 0


def test_the_read_challenge_expiry_is_coarse(client, settled, empty_table):
    """R09: the read mint is idempotent inside its window, so an exact expiry
    would tell anyone who can name a task when its payer last started reading.
    Floored to the minute, like the dispute mint's — and never later."""
    answered = _challenge(client)["expires_at"]
    real = empty_table[(TASK, eb.DISPUTE_READ_SUBJECT)][1]

    assert answered == math.floor(real / 60) * 60
    assert answered <= real


def test_a_grant_is_dead_at_its_expiry_second():
    """T04: `expires_at` is the first second the grant no longer works."""
    grant, expires_at = task_auth.mint_read_grant(TASK, PAYER.public_key)

    assert task_auth.read_grant_admits(grant, TASK, PAYER.public_key, now=expires_at - 1) is True
    assert task_auth.read_grant_admits(grant, TASK, PAYER.public_key, now=expires_at) is False


def _compare_digest_spy(monkeypatch, owner) -> list[tuple[object, object]]:
    """Record every constant-time compare made through `owner.compare_digest`.

    A timing property cannot be seen by a functional test, so these assert the
    compare is MADE through `compare_digest` — `==` in its place passes every
    other test in the suite."""
    calls: list[tuple[object, object]] = []
    real = owner.compare_digest

    def _spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(owner, "compare_digest", _spy)
    return calls


def test_the_grant_mac_is_compared_in_constant_time(monkeypatch):
    """T06: the MAC is compared before any field in the grant is believed."""
    grant, _ = task_auth.mint_read_grant(TASK, PAYER.public_key)
    calls = _compare_digest_spy(monkeypatch, task_auth.hmac)

    assert task_auth.read_grant_admits(grant, TASK, PAYER.public_key) is True
    assert any(isinstance(a, bytes) and len(a) == 32 and len(b) == 32 for a, b in calls)


def test_the_read_nonce_is_compared_in_constant_time(monkeypatch, empty_table):
    """E09."""
    nonce, _ = eb.issue_dispute_read_challenge(TASK)
    calls = _compare_digest_spy(monkeypatch, eb.secrets)

    assert eb.dispute_read_challenge_state(TASK, nonce) == "live"
    assert (nonce, nonce) in calls
