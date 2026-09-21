"""HTTP-level tests for the dispute window's four routes (story 4.02).

The rules — who may dispute, whether the window is open, what a step was
charged — belong to `services/dispute_svc.py` and are tested against that
service. What is tested HERE is the wire, and only the wire:

  * a body the service should never see is refused by validation FIRST, so a
    malformed request costs no settlement read;
  * every frozen refusal arrives as the right status with `error.code` set to
    the service's own token, verbatim;
  * `duplicate_dispute` answers with the original dispute — the one acceptance
    criterion the shared error envelope has no room to express;
  * an unsettled task reads as an empty window rather than an error.

Every test stubs `dispute_svc` at the seam the router imported. That keeps this
file passing without the rules lane's implementation present, and keeps it
passing when that implementation changes: the contract between the two lanes is
the function signature and the exception's attributes, and those are all these
tests touch.
"""

from __future__ import annotations

import base64

import pytest

from app.services import dispute_svc
from app.services.dispute_store import DisputeRecord, SettlementRecord, SettlementStep

JOB_ID = "1234567890abcdef1234567890abcdef"
PAYER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
# 64 bytes base64-encoded — the shape an ed25519 signature actually has, so the
# edge bounds are exercised by something the verifier could plausibly be given.
SIGNATURE = base64.b64encode(b"s" * 64).decode("ascii")
NONCE = "0123456789abcdef0123456789abcdef"


def open_body(**overrides: object) -> dict:
    """A well-formed `POST /api/disputes` body, with fields swapped per test."""
    body: dict = {
        "job_id_hex": JOB_ID,
        "step_index": 1,
        "reason": "the step returned an empty file",
        "payer": PAYER,
        "nonce": NONCE,
        "signature_b64": SIGNATURE,
    }
    body.update(overrides)
    return body


def record(**overrides: object) -> DisputeRecord:
    """A stored dispute, as the service would hand one back."""
    fields: dict = {
        "id": "dsp_00112233445566778",
        "job_id_hex": JOB_ID,
        "task_id": "task-1",
        "step_index": 1,
        "agent_id": "code-agent",
        "payer": PAYER,
        "reason": "the step returned an empty file",
        "status": "open",
        "charged_usdc": 0.25,
        "creditable_usdc": 0.25,
        "opened_at": 1_700_000_000.0,
    }
    fields.update(overrides)
    return DisputeRecord(**fields)


def dispute_error(code: str, status: int, *, existing: DisputeRecord | None = None) -> Exception:
    """A `DisputeError` built by ATTRIBUTE, never by constructor.

    The frozen contract between this lane and the rules lane fixes the
    exception's attributes — `.code`, `.message`, `.status_code`, `.existing` —
    and says nothing about its `__init__`. Constructing one positionally here
    would couple these tests to a signature the other lane is free to shape,
    and would fail for a reason that has nothing to do with the routes.
    """
    exc = dispute_svc.DisputeError.__new__(dispute_svc.DisputeError)
    Exception.__init__(exc, code)
    exc.code = code
    exc.message = code.replace("_", " ")
    exc.status_code = status
    exc.existing = existing
    return exc


def never_called(name: str):
    """A stub that fails the test if the route reaches it."""

    async def _fail(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{name} was called; the request should have been refused before it")

    return _fail


@pytest.fixture()
def challenge_stub(monkeypatch):
    """Point the challenge mint at a known nonce; record every call."""
    calls: list[tuple[str, int]] = []

    async def _issue(job_id_hex: str, step_index: int) -> tuple[str, float]:
        calls.append((job_id_hex, step_index))
        return NONCE, 1_700_000_300.0

    monkeypatch.setattr(dispute_svc, "issue_dispute_challenge", _issue)
    return calls


# ── the challenge mint ──────────────────────────────────────────


def test_challenge_returns_the_message_the_wallet_must_sign(client, challenge_stub):
    r = client.post("/api/disputes/challenge", json={"job_id_hex": JOB_ID, "step_index": 1})

    assert r.status_code == 200
    body = r.json()
    assert body["nonce"] == NONCE
    assert body["expires_at"] == 1_700_000_300.0
    # Derived from the service, not assembled in the router — so the domain
    # separator and the field order can only ever have one definition.
    assert body["message"] == dispute_svc.dispute_message(JOB_ID, 1, NONCE)
    assert body["message"].startswith(dispute_svc.DISPUTE_MESSAGE_PREFIX)
    assert challenge_stub == [(JOB_ID, 1)]


def test_challenge_delegates_the_whole_mint_to_the_service(client, challenge_stub, monkeypatch):
    # Whether a mint is allowed is the service's call — it reads the settlement
    # itself, so its bounded challenge table can only hold pairs that could
    # really be disputed. The route must not form a second opinion by reaching
    # for the settlement or the dispute list on its own.
    monkeypatch.setattr(dispute_svc, "settlement_for_task", never_called("settlement_for_task"))
    monkeypatch.setattr(dispute_svc, "list_for_task", never_called("list_for_task"))
    monkeypatch.setattr(dispute_svc, "open_dispute", never_called("open_dispute"))

    r = client.post("/api/disputes/challenge", json={"job_id_hex": JOB_ID, "step_index": 0})

    assert r.status_code == 200
    assert len(challenge_stub) == 1


def test_challenge_surfaces_a_service_refusal_in_the_envelope(client, monkeypatch):
    # The route checks nothing itself, but if the rules lane ever does refuse a
    # mint, that refusal must arrive as its own code — never as a 500.
    async def _refuse(job_id_hex: str, step_index: int) -> tuple[str, float]:
        raise dispute_error("unknown_job", 404)

    monkeypatch.setattr(dispute_svc, "issue_dispute_challenge", _refuse)

    r = client.post("/api/disputes/challenge", json={"job_id_hex": JOB_ID, "step_index": 0})

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_job"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id_hex", "nope"),
        ("job_id_hex", "1234567890abcdef1234567890abcde"),  # 31 chars
        ("step_index", -1),
        ("step_index", 64),
    ],
    ids=["job-id-not-hex", "job-id-too-short", "step-negative", "step-above-cap"],
)
def test_challenge_validates_before_minting_anything(client, challenge_stub, field, value):
    body = {"job_id_hex": JOB_ID, "step_index": 1}
    body[field] = value

    r = client.post("/api/disputes/challenge", json=body)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert challenge_stub == []


# ── opening a dispute ───────────────────────────────────────────


def opens_with(monkeypatch, result: DisputeRecord | Exception) -> list[dict]:
    """Point the router's `open_dispute` at one outcome; record every call."""
    calls: list[dict] = []

    async def _open(**kwargs: object) -> DisputeRecord:
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(dispute_svc, "open_dispute", _open)
    return calls


def test_a_successful_dispute_is_200_and_open(client, monkeypatch):
    calls = opens_with(monkeypatch, record())

    r = client.post("/api/disputes", json=open_body())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "open"
    assert body["id"] == "dsp_00112233445566778"
    assert body["task_id"] == "task-1"
    assert body["charged_usdc"] == 0.25
    assert body["creditable_usdc"] == 0.25
    # Nothing is resolved yet, and the wire says so rather than omitting it.
    assert body["resolved_at"] is None
    assert body["refund_tx"] is None
    # Every field arrives at the service by keyword, unmangled — the router
    # renames nothing, which is what lets the frozen signature be frozen.
    assert calls == [
        {
            "job_id_hex": JOB_ID,
            "step_index": 1,
            "reason": "the step returned an empty file",
            "payer": PAYER,
            "nonce": NONCE,
            "signature_b64": SIGNATURE,
        }
    ]


# Every refusal the rules lane can answer with, and the status it carries. The
# codes are the frozen half of the 4.02 contract: the console maps each one to
# a sentence, so a code that silently changed would surface as an unexplained
# error in the buyer's face.
FROZEN_REFUSALS = [
    ("unknown_job", 404),
    ("not_the_payer", 403),
    ("dispute_window_closed", 409),
    ("step_not_settled", 409),
    ("nothing_was_charged", 409),
    ("signature_malformed", 400),
    ("challenge_expired", 400),
]


@pytest.mark.parametrize(("code", "status"), FROZEN_REFUSALS, ids=[c for c, _ in FROZEN_REFUSALS])
def test_a_refusal_keeps_its_code_and_its_status(client, monkeypatch, code, status):
    opens_with(monkeypatch, dispute_error(code, status))

    r = client.post("/api/disputes", json=open_body())

    assert r.status_code == status
    body = r.json()
    # Verbatim in BOTH halves of the envelope: `error.code` is what the console
    # switches on, `detail` is what the pre-envelope clients still read.
    assert body["error"]["code"] == code
    assert body["detail"] == code
    assert "dispute" not in body


def test_a_duplicate_answers_with_the_original_dispute_unchanged(client, monkeypatch):
    original = record(id="dsp_first", reason="the first thing I said", opened_at=1_699_000_000.0)
    opens_with(monkeypatch, dispute_error("duplicate_dispute", 409, existing=original))

    r = client.post("/api/disputes", json=open_body(reason="a second, different complaint"))

    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "duplicate_dispute"
    # The FIRST dispute, not the second attempt's text: a repeat must not be
    # able to rewrite what the buyer originally filed.
    assert body["dispute"]["id"] == "dsp_first"
    assert body["dispute"]["reason"] == "the first thing I said"
    assert body["dispute"]["opened_at"] == 1_699_000_000.0
    assert body["dispute"]["status"] == "open"


def test_the_duplicate_body_is_the_error_envelope_plus_the_dispute(client, monkeypatch):
    # `_duplicate_envelope` assembles this body by hand because the shared
    # handler in app/main.py cannot carry a payload. Pin the two shapes
    # together here, or they drift the first time the envelope changes.
    opens_with(monkeypatch, dispute_error("unknown_job", 404))
    plain = client.post("/api/disputes", json=open_body()).json()

    opens_with(monkeypatch, dispute_error("duplicate_dispute", 409, existing=record()))
    duplicate = client.post("/api/disputes", json=open_body()).json()

    assert set(duplicate) == set(plain) | {"dispute"}
    assert set(duplicate["error"]) == set(plain["error"])
    assert duplicate["error"]["request_id"]
    assert duplicate["error"]["message"] == "duplicate dispute"


# Bodies the rules lane must never be asked about. Each one is refused by the
# edge, so a malformed request costs no settlement read and no signature
# verification — and the caller gets the field-level `validation_error` list
# rather than a code invented for it here.
MALFORMED_BODIES = [
    ("job-id-not-hex", {"job_id_hex": "zzzz567890abcdef1234567890abcdef"}),
    ("job-id-too-long", {"job_id_hex": JOB_ID + "ab"}),
    ("step-negative", {"step_index": -1}),
    ("step-above-cap", {"step_index": 64}),
    ("payer-not-an-address", {"payer": "not-an-address"}),
    ("payer-wrong-prefix", {"payer": "M" + PAYER[1:]}),
    ("reason-empty", {"reason": ""}),
    ("reason-too-long", {"reason": "x" * (dispute_svc.MAX_REASON_CHARS + 1)}),
    # The paragraph the old 2,000-character edge let through and the service
    # then cut to its first 500 without a word: refused now, never trimmed.
    ("reason-a-paragraph-over", {"reason": "x" * 1500}),
    ("signature-too-long", {"signature_b64": "x" * 257}),
    ("nonce-too-long", {"nonce": "x" * 129}),
]


@pytest.mark.parametrize(("label", "overrides"), MALFORMED_BODIES, ids=[label for label, _ in MALFORMED_BODIES])
def test_a_malformed_body_never_reaches_the_service(client, monkeypatch, label, overrides):
    calls = opens_with(monkeypatch, record())

    r = client.post("/api/disputes", json=open_body(**overrides))

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert calls == []


def test_a_missing_field_never_reaches_the_service(client, monkeypatch):
    calls = opens_with(monkeypatch, record())
    body = open_body()
    del body["signature_b64"]

    r = client.post("/api/disputes", json=body)

    assert r.status_code == 422
    assert calls == []


# ── reading disputes back ───────────────────────────────────────


def settlement(**overrides: object) -> SettlementRecord:
    """A settled workflow, as the store recorded it at settlement time."""
    fields: dict = {
        "task_id": "task-1",
        "payer": PAYER,
        "auth_id_hex": "fedcba0987654321fedcba0987654321",
        "job_id_hex": JOB_ID,
        "charge_tx": "abc123",
        "proof_tx": "def456",
        "settled_usdc": 0.5,
        "steps": (
            SettlementStep(step_index=1, agent_id="code-agent", agent_name="Coder", price_usdc=0.25, delivered=True),
        ),
        "settled_at": 1_700_000_000.0,
        "window_closes_at": 1_700_086_400.0,
    }
    fields.update(overrides)
    return SettlementRecord(**fields)


def reads(monkeypatch, *, dispute: DisputeRecord | None = None) -> None:
    """Point `GET /api/disputes/{id}` at one answer."""

    async def _get(dispute_id: str) -> DisputeRecord | None:
        return dispute

    monkeypatch.setattr(dispute_svc, "get_dispute", _get)


def lists(monkeypatch, *, found: SettlementRecord | None, disputes: tuple[DisputeRecord, ...]) -> None:
    """Point `GET /api/tasks/{id}/disputes` at one settlement and its disputes."""

    async def _settlement(task_id: str) -> SettlementRecord | None:
        return found

    async def _disputes(task_id: str) -> tuple[DisputeRecord, ...]:
        return disputes

    monkeypatch.setattr(dispute_svc, "settlement_for_task", _settlement)
    monkeypatch.setattr(dispute_svc, "list_for_task", _disputes)


def test_reading_a_dispute_returns_it(client, monkeypatch):
    reads(monkeypatch, dispute=record(id="dsp_readable"))

    r = client.get("/api/disputes/dsp_readable")

    assert r.status_code == 200
    assert r.json()["id"] == "dsp_readable"
    assert r.json()["status"] == "open"


def test_reading_an_unknown_dispute_is_404(client, monkeypatch):
    reads(monkeypatch, dispute=None)

    r = client.get("/api/disputes/dsp_nothing_here")

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_dispute"


def test_the_task_listing_returns_the_window_and_what_was_raised(client, monkeypatch):
    lists(monkeypatch, found=settlement(), disputes=(record(id="dsp_a"), record(id="dsp_b", step_index=2)))

    r = client.get("/api/tasks/task-1/disputes")

    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "task-1"
    # The deadline the buyer was given, read off the settlement record — not
    # recomputed from DISPUTE_WINDOW_SECONDS, which may since have been retuned.
    assert body["window_closes_at"] == 1_700_086_400.0
    assert [d["id"] for d in body["disputes"]] == ["dsp_a", "dsp_b"]
    assert [d["step_index"] for d in body["disputes"]] == [1, 2]


def test_the_task_listing_is_an_empty_window_before_settlement(client, monkeypatch):
    # A running or unpaid task: nothing to dispute, no deadline, and NOT a 404
    # — the console polls this route while the workflow is still going.
    lists(monkeypatch, found=None, disputes=())

    r = client.get("/api/tasks/task-unsettled/disputes")

    assert r.status_code == 200
    body = r.json()
    # The clock is present even with nothing to count down to, so the console
    # can take its skew from any read rather than only a settled one.
    assert isinstance(body.pop("now"), float)
    assert body == {"task_id": "task-unsettled", "window_closes_at": None, "settlement": None, "disputes": []}
