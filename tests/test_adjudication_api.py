"""HTTP-level tests for the adjudication pair (story 4.03).

`POST /api/disputes/{id}/uphold` and `/reject` are the two routes in this API
that spend the platform's OWN settler balance, or close a claim against it for
good. What is tested here is the wire and the door — and only those:

  * the door refuses in all three of its ways, and refuses BEFORE the service
    is reached, so a closed deployment costs no store read and signs nothing;
  * with the key, the door admits — a guard that only ever refuses is
    indistinguishable from a broken route;
  * a credited uphold reaches the client as `credited` with its `refund_tx`,
    which is the receipt story 4.06 renders;
  * a REPEAT uphold answers with the same hash rather than a 5xx, because an
    adjudicator who double-clicks must see what happened, not an error;
  * every refusal arrives as the service's own code and status, verbatim.

Whether a dispute may be upheld, how much is creditable, and — the part that
must never have two homes — whether a transfer has already been claimed for it
all belong to `services/dispute_svc.py` and are tested against that service.
Every test here stubs it at the seam the router imported, exactly as
`tests/test_dispute_api.py` does, so this file pins the routes rather than the
rules and keeps passing as the rules move.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import dispute_svc
from app.services.dispute_store import DisputeRecord

JOB_ID = "1234567890abcdef1234567890abcdef"
PAYER = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"
DISPUTE_ID = "dsp_00112233445566778"
# A 64-hex Stellar transaction hash — the shape `execute_refund` hands back, so
# the receipt assertions are made against something the wire will really carry.
REFUND_TX = "3f1b" + "0" * 60

API_KEY = "operator-secret-key"
AUTH = {"X-API-Key": API_KEY}

# A reason written for the buyer, because that is who reads it: the note is
# required and comes back as the dispute's `rejection_reason`, so every
# rejection this file expects to reach the service sends one.
NOTE = "the delivered file matched the brief"

UPHOLD = f"/api/disputes/{DISPUTE_ID}/uphold"
REJECT = f"/api/disputes/{DISPUTE_ID}/reject"
ROUTES = [UPHOLD, REJECT]


def record(**overrides: object) -> DisputeRecord:
    """A stored dispute, as the service would hand one back."""
    fields: dict = {
        "id": DISPUTE_ID,
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


def credited(**overrides: object) -> DisputeRecord:
    """The terminal record of an upheld dispute: paid, hashed, resolved."""
    fields: dict = {
        "status": "credited",
        "refund_tx": REFUND_TX,
        "resolved_at": 1_700_000_500.0,
    }
    fields.update(overrides)
    return record(**fields)


def dispute_error(code: str, status: int) -> Exception:
    """A `DisputeError` built by ATTRIBUTE, never by constructor.

    `tests/test_dispute_api.py` carries the full reasoning; in short, the
    frozen contract between this lane and the rules lane fixes the exception's
    attributes and says nothing about its `__init__`, so constructing one
    positionally would couple these tests to a signature that lane may shape.
    """
    exc = dispute_svc.DisputeError.__new__(dispute_svc.DisputeError)
    Exception.__init__(exc, code)
    exc.code = code
    exc.message = code.replace("_", " ")
    exc.status_code = status
    exc.existing = None
    return exc


@pytest.fixture()
def adjudicating(hermetic_settings, monkeypatch):
    """A deployment that CAN adjudicate: master switch on, operator key set.

    The switch goes through monkeypatch rather than through
    `hermetic_settings`, which does not save or restore it — a test that left
    the refund path armed would arm it for everything that ran afterwards in
    the same process. Setting it explicitly in both directions also makes
    every test here independent of whatever a developer's `.env` says.
    """
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    hermetic_settings.api_key = API_KEY
    return hermetic_settings


def upholds_with(monkeypatch, result: DisputeRecord | Exception) -> list[str]:
    """Point the router's `uphold` at one outcome; record every call."""
    calls: list[str] = []

    async def _uphold(dispute_id: str) -> DisputeRecord:
        calls.append(dispute_id)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(dispute_svc, "uphold", _uphold)
    return calls


def rejects_with(monkeypatch, result: DisputeRecord | Exception) -> list[tuple[str, str]]:
    """Point the router's `reject` at one outcome; record id and note.

    The stub takes `note` as the service does — keyword-only and with no
    default — so a router that stopped passing it would fail here with a
    TypeError rather than quietly reject with nothing to show the buyer.
    """
    calls: list[tuple[str, str]] = []

    async def _reject(dispute_id: str, *, note: str) -> DisputeRecord:
        calls.append((dispute_id, note))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(dispute_svc, "reject", _reject)
    return calls


def sealed(monkeypatch) -> list[str]:
    """Stub BOTH service entry points to fail the test if either is reached.

    Shared by every refusal test below, because "the door refused" and "the
    door refused before anything was read or signed" are different claims and
    only the second one is worth having on a money path.
    """
    reached: list[str] = []

    async def _uphold(dispute_id: str) -> DisputeRecord:
        reached.append("uphold")
        raise AssertionError("uphold reached the service; the guard should have refused first")

    async def _reject(dispute_id: str, *, note: str) -> DisputeRecord:
        reached.append("reject")
        raise AssertionError("reject reached the service; the guard should have refused first")

    monkeypatch.setattr(dispute_svc, "uphold", _uphold)
    monkeypatch.setattr(dispute_svc, "reject", _reject)
    return reached


# ── the door: it refuses ────────────────────────────────────────


@pytest.mark.parametrize("path", ROUTES, ids=["uphold", "reject"])
def test_adjudication_is_503_while_the_refund_switch_is_off(client, hermetic_settings, monkeypatch, path):
    # The correct key is supplied, so the refusal can only be the switch. A
    # deployment that has not turned the refund path on must not be able to pay
    # a dispute out — nor to dispose of one, which is why reject is here too.
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)
    hermetic_settings.api_key = API_KEY
    reached = sealed(monkeypatch)

    r = client.post(path, json={}, headers=AUTH)

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "dispute_refunds_disabled"
    assert reached == []


@pytest.mark.parametrize("path", ROUTES, ids=["uphold", "reject"])
def test_adjudication_is_503_when_no_api_key_is_configured(client, hermetic_settings, monkeypatch, path):
    """The fail-closed test — the reason `require_adjudicator` exists at all.

    `require_api_key` answers an unset API_KEY by returning, which is safe for
    the demo surface and would be a catastrophe here: the switch is on, so
    these routes can sign, and a fall-through would make the settler wallet
    drainable by anyone who could reach the port. The config validator does
    not close this either — it only demands API_KEY once a signing key AND an
    asset SAC are also set, so this exact configuration boots.
    """
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    hermetic_settings.api_key = ""
    reached = sealed(monkeypatch)

    r = client.post(path, json={})

    assert r.status_code == 503, "an unset API_KEY fell through to the refund path"
    assert r.json()["error"]["code"] == "adjudication_not_configured"
    assert reached == []


def test_an_unconfigured_key_refuses_even_when_the_caller_sends_one(client, hermetic_settings, monkeypatch):
    # The obvious wrong implementation — compare the header against whatever is
    # configured — admits every caller when nothing is configured, because ""
    # is trivially guessable. Pin that it is the CONFIGURATION that closes the
    # door, not the absence of a header.
    monkeypatch.setattr(settings, "dispute_refunds_enabled", True)
    hermetic_settings.api_key = ""
    reached = sealed(monkeypatch)

    r = client.post(UPHOLD, json={}, headers={"X-API-Key": ""})

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "adjudication_not_configured"
    assert reached == []


def test_the_switch_is_checked_before_the_key(client, hermetic_settings, monkeypatch):
    # Both are wrong: no key configured, none supplied, switch off. The answer
    # names the switch, because that is the operator's first question — "is
    # this deployment supposed to adjudicate at all?" — and answering "wrong
    # key" would send them hunting for a credential they do not need yet.
    monkeypatch.setattr(settings, "dispute_refunds_enabled", False)
    hermetic_settings.api_key = ""

    r = client.post(UPHOLD, json={})

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "dispute_refunds_disabled"


@pytest.mark.parametrize("path", ROUTES, ids=["uphold", "reject"])
def test_a_missing_key_is_401(client, adjudicating, monkeypatch, path):
    reached = sealed(monkeypatch)

    r = client.post(path, json={})

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"
    assert reached == []


@pytest.mark.parametrize("path", ROUTES, ids=["uphold", "reject"])
def test_a_wrong_key_is_401(client, adjudicating, monkeypatch, path):
    reached = sealed(monkeypatch)

    r = client.post(path, json={}, headers={"X-API-Key": "not-the-operator-key"})

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"
    assert reached == []


def test_a_prefix_of_the_key_is_401(client, adjudicating, monkeypatch):
    # A length-only or prefix-only comparison would admit this. `compare_digest`
    # over the full bytes is what does not.
    sealed(monkeypatch)

    r = client.post(UPHOLD, json={}, headers={"X-API-Key": API_KEY[:-1]})

    assert r.status_code == 401


# Header bytes that are NOT ascii, sent as bytes because that is the only way
# they can arrive: Starlette decodes every header latin-1, so these reach the
# guard as a str with characters outside ascii. `secrets.compare_digest` raises
# TypeError on such a str, so a guard comparing str rather than utf-8 bytes
# turns a bad key into a 500 — an unhandled-exception oracle on the one route
# that pays out. The refusal must be an ordinary 401.
NON_ASCII_KEYS = [
    ("latin1-accents", "clé-secrète".encode()),
    ("high-bytes", b"\xff\xfe\xfd\xfc"),
    ("utf8-cyrillic", "ключ-оператора".encode()),
    ("utf8-emoji", "operator-\U0001f511".encode()),
]


@pytest.mark.parametrize(("label", "raw"), NON_ASCII_KEYS, ids=[label for label, _ in NON_ASCII_KEYS])
def test_a_non_ascii_key_is_a_401_and_never_a_500(client, adjudicating, monkeypatch, label, raw):
    reached = sealed(monkeypatch)

    r = client.post(UPHOLD, json={}, headers={"X-API-Key": raw})

    assert r.status_code == 401, f"a {label} key answered {r.status_code}; the guard compared str, not bytes"
    assert r.json()["error"]["code"] == "invalid_api_key"
    assert reached == []


@pytest.mark.parametrize(
    ("label", "configured"),
    [("latin1-accents", "passphrase-naïve"), ("outside-latin1", "passphrase-Ω")],
    ids=["latin1-accents", "outside-latin1"],
)
def test_a_non_ascii_configured_key_locks_the_door_rather_than_crashing(
    client, adjudicating, monkeypatch, label, configured
):
    """The other side of the same hazard: a non-ascii value pasted into API_KEY.

    `expected.encode("utf-8")` cannot raise, so the guard does not crash on
    this side — but the header round-trip is lossy either way (Starlette
    decodes latin-1; httpx's ASGI transport, which `client` rides on, encodes
    every header utf-8 before that), so such a key does not in practice match
    anything a client can send. That makes the configuration unusable, and
    what matters on a payout route is HOW it is unusable: every attempt must
    be an ordinary 401, so the operator sees a locked door in their access log
    and goes looking at API_KEY, rather than a stream of 500s from an
    unhandled TypeError that reads like the service itself is broken.

    Fail-closed is the correct end state here, so this pins the refusal rather
    than chasing an encoding that would admit the caller. What proves the
    comparison is a real comparison and not a blanket refusal is the ascii
    pair above: `test_the_key_admits_the_caller_to_uphold` admits the right
    key, `test_a_prefix_of_the_key_is_401` refuses one byte short of it.
    """
    adjudicating.api_key = configured
    reached = sealed(monkeypatch)

    same_bytes = client.post(UPHOLD, json={}, headers={"X-API-Key": configured.encode()})
    ascii_fold = client.post(UPHOLD, json={}, headers={"X-API-Key": b"passphrase-"})

    assert same_bytes.status_code == 401, f"a {label} configured key answered {same_bytes.status_code}, not 401"
    assert same_bytes.json()["error"]["code"] == "invalid_api_key"
    assert ascii_fold.status_code == 401
    assert reached == []


# ── the door: it admits ─────────────────────────────────────────


def test_the_key_admits_the_caller_to_uphold(client, adjudicating, monkeypatch):
    # A guard that only ever refuses is indistinguishable from a broken route,
    # so pin that the correct key reaches the service with the id from the path.
    calls = upholds_with(monkeypatch, credited())

    r = client.post(UPHOLD, json={}, headers=AUTH)

    assert r.status_code == 200, r.text
    assert calls == [DISPUTE_ID]


def test_the_key_admits_the_caller_to_reject(client, adjudicating, monkeypatch):
    # With a note, because a rejection without one is refused at the edge: the
    # only thing this may pin is the door, so the body has to be one the door
    # would otherwise admit.
    calls = rejects_with(monkeypatch, record(status="rejected", resolved_at=1_700_000_500.0))

    r = client.post(REJECT, json={"note": NOTE}, headers=AUTH)

    assert r.status_code == 200, r.text
    assert calls == [(DISPUTE_ID, NOTE)]


# ── upholding ───────────────────────────────────────────────────


def test_a_successful_uphold_is_credited_with_a_refund_tx(client, adjudicating, monkeypatch):
    upholds_with(monkeypatch, credited())

    r = client.post(UPHOLD, json={}, headers=AUTH)

    assert r.status_code == 200, r.text
    body = r.json()
    # The two fields story 4.06 renders the buyer's receipt from. Asserted by
    # name on the wire, not on the record: a response model that stopped
    # projecting either would still pass every test that reads the record.
    assert body["status"] == "credited"
    assert body["refund_tx"] == REFUND_TX
    assert body["resolved_at"] == 1_700_000_500.0
    assert body["id"] == DISPUTE_ID
    assert body["payer"] == PAYER
    assert body["creditable_usdc"] == 0.25


def test_a_repeat_uphold_returns_the_same_hash(client, adjudicating, monkeypatch):
    """Double-click safety, as far as this layer can pin it.

    The claim in `dispute_store` is what actually makes the payout happen
    once; what the ROUTE owes is to hand the service's terminal answer back
    unchanged on the second call rather than inventing an error from it. So a
    repeat is 200 with the same `refund_tx`, and above all not a 5xx — an
    adjudicator retrying a dropped response must be shown what happened, not
    told the platform broke.
    """
    terminal = credited()
    calls = upholds_with(monkeypatch, terminal)

    first = client.post(UPHOLD, json={}, headers=AUTH)
    second = client.post(UPHOLD, json={}, headers=AUTH)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.status_code < 500
    assert second.json()["refund_tx"] == first.json()["refund_tx"] == REFUND_TX
    assert second.json()["status"] == first.json()["status"] == "credited"
    # Identical bodies, not merely identical hashes: a repeat must not resolve
    # the dispute a second time or move the timestamp the buyer was shown.
    assert second.json() == first.json()
    assert calls == [DISPUTE_ID, DISPUTE_ID]


# Every refusal the adjudication lane can answer with today, and the status
# each carries. The router holds NO mapping table — `_refuse` passes the
# service's own code and status straight through — so what is really pinned
# here is that passthrough, and a code the rules lane adds tomorrow needs no
# change in this layer to reach the frontend intact. A code that arrived as a
# 500, or that was rewritten on the way out, would be a bug here.
#
# The same list is run against BOTH routes even though not every code arises
# on both — `refund_above_cap` cannot come from a rejection — precisely
# because the routes must not hold opinions about which codes are theirs.
REFUSALS = [
    ("unknown_dispute", 404),
    ("dispute_not_open", 409),
    ("dispute_rejected", 409),
    ("refund_in_flight", 409),
    ("settlement_missing", 409),
    ("nothing_to_credit", 409),
    ("refund_above_cap", 409),
    ("refund_failed", 502),
    # D3's timeout: submitted, may still land, never auto-retried. It reaches
    # the adjudicator as a 504 rather than a 500 because the platform knows
    # exactly what happened and is saying so.
    ("refund_unconfirmed", 504),
    # A rejection note that cleaning left empty — whitespace, or nothing but
    # control characters. The edge cannot see that; only the service cleans,
    # so only the service can refuse it.
    ("rejection_reason_required", 422),
]


@pytest.mark.parametrize(("code", "status"), REFUSALS, ids=[c for c, _ in REFUSALS])
def test_an_uphold_refusal_keeps_its_code_and_its_status(client, adjudicating, monkeypatch, code, status):
    upholds_with(monkeypatch, dispute_error(code, status))

    r = client.post(UPHOLD, json={}, headers=AUTH)

    assert r.status_code == status
    body = r.json()
    # Verbatim in BOTH halves of the envelope, as every other dispute refusal
    # arrives: `error.code` is what the console switches on, `detail` is what
    # the pre-envelope clients still read.
    assert body["error"]["code"] == code
    assert body["detail"] == code


@pytest.mark.parametrize(("code", "status"), REFUSALS, ids=[c for c, _ in REFUSALS])
def test_a_reject_refusal_keeps_its_code_and_its_status(client, adjudicating, monkeypatch, code, status):
    # A valid note, so the edge admits the body and the only answer left is
    # the service's own.
    rejects_with(monkeypatch, dispute_error(code, status))

    r = client.post(REJECT, json={"note": NOTE}, headers=AUTH)

    assert r.status_code == status
    assert r.json()["error"]["code"] == code


# ── rejecting ───────────────────────────────────────────────────


def test_a_successful_reject_is_rejected_and_resolved(client, adjudicating, monkeypatch):
    rejects_with(monkeypatch, record(status="rejected", resolved_at=1_700_000_500.0, note=NOTE))

    r = client.post(REJECT, json={"note": NOTE}, headers=AUTH)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert body["resolved_at"] == 1_700_000_500.0
    # Nothing was paid, and the wire says so rather than omitting the field.
    assert body["refund_tx"] is None
    assert body["credited_usdc"] is None
    # The adjudicator's answer comes straight back as the buyer will read it,
    # so the console that wrote it sees exactly what it published.
    assert body["rejection_reason"] == NOTE


def test_the_note_reaches_the_service_by_keyword(client, adjudicating, monkeypatch):
    # By keyword and unmangled: the router renames nothing, which is what lets
    # the service's signature be the frozen half of the contract.
    calls = rejects_with(monkeypatch, record(status="rejected"))

    client.post(REJECT, json={"note": "the delivered file matched the brief"}, headers=AUTH)

    assert calls == [(DISPUTE_ID, "the delivered file matched the brief")]


# Every way a client can say "no note", and one answer to all of them. 4.03
# pinned that however a note is absent it is absent the same way, and that
# still holds — but the answer is now a refusal, because the note is what the
# buyer is shown and a rejection with nothing to show them is not one. The
# empty string moved here from the malformed notes below: it is not a note
# of the wrong shape, it is no note at all.
NO_NOTE = [
    ("no-body-at-all", None),
    ("empty-object", {}),
    ("explicit-null", {"note": None}),
    ("empty-string", {"note": ""}),
]


@pytest.mark.parametrize(("label", "payload"), NO_NOTE, ids=[label for label, _ in NO_NOTE])
def test_a_rejection_without_a_note_never_reaches_the_service(client, adjudicating, monkeypatch, label, payload):
    calls = rejects_with(monkeypatch, record(status="rejected"))

    r = client.post(REJECT, headers=AUTH) if payload is None else client.post(REJECT, json=payload, headers=AUTH)

    assert r.status_code == 422, f"a rejection with {label} was admitted"
    # The field-level code, not the service's `rejection_reason_required`:
    # that one answers a note the edge admitted and cleaning emptied, and
    # seeing it here would mean the service had been asked.
    assert r.json()["error"]["code"] == "validation_error"
    assert calls == []


# Notes the service must never be asked about, refused at the edge with the
# field-level `validation_error` the frontend can render inline — bounded
# exactly as `OpenDisputeReq.reason` is, because it is the same kind of text.
MALFORMED_NOTES = [
    # One over the service's own ceiling, which is the only bound there is:
    # anything longer would reach the buyer cut short, so the edge refuses it
    # and the adjudicator is the one who shortens it.
    ("note-one-over-the-bound", {"note": "x" * (dispute_svc.MAX_REASON_CHARS + 1)}),
    ("note-not-a-string", {"note": 7}),
]


@pytest.mark.parametrize(("label", "payload"), MALFORMED_NOTES, ids=[label for label, _ in MALFORMED_NOTES])
def test_a_malformed_note_never_reaches_the_service(client, adjudicating, monkeypatch, label, payload):
    calls = rejects_with(monkeypatch, record(status="rejected"))

    r = client.post(REJECT, json=payload, headers=AUTH)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert calls == []


@pytest.mark.parametrize(
    ("label", "note"),
    [("whitespace", "   \n\t "), ("control-characters", "\x00\x07\x1b")],
    ids=["whitespace", "control-characters"],
)
def test_a_note_that_cleans_to_nothing_is_the_services_422(client, adjudicating, monkeypatch, label, note):
    """The half of the rule the edge cannot enforce, and must not try to.

    `min_length` counts characters before cleaning, so a note of blanks is a
    note to the edge and reaches the service byte for byte — the router trims
    nothing, or the service's cleaning would stop being the one definition of
    "empty". The service's refusal then arrives as its own code at 422,
    distinct from `validation_error`, so the console can say "that reason is
    blank" rather than "the request was malformed".
    """
    calls = rejects_with(monkeypatch, dispute_error("rejection_reason_required", 422))

    r = client.post(REJECT, json={"note": note}, headers=AUTH)

    assert calls == [(DISPUTE_ID, note)], f"a {label} note did not reach the service unchanged"
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "rejection_reason_required"
    assert r.json()["detail"] == "rejection_reason_required"


def test_a_note_at_the_bound_is_accepted(client, adjudicating, monkeypatch):
    # The bound is inclusive, as `OpenDisputeReq.reason`'s is — pinned so a
    # future tightening is a deliberate change rather than an off-by-one. It is
    # the SERVICE's ceiling, not a number restated here: story 4.05 found the
    # edge accepting 2000 characters that `reject` then silently trimmed to
    # this, so the edge now refuses what the service would cut.
    calls = rejects_with(monkeypatch, record(status="rejected"))
    at_the_bound = "x" * dispute_svc.MAX_REASON_CHARS

    r = client.post(REJECT, json={"note": at_the_bound}, headers=AUTH)

    assert r.status_code == 200, r.text
    assert calls == [(DISPUTE_ID, at_the_bound)]


# ── the path parameter ──────────────────────────────────────────


@pytest.mark.parametrize("path", ROUTES, ids=["uphold", "reject"])
def test_an_oversized_dispute_id_never_reaches_the_service(client, adjudicating, monkeypatch, path):
    # 64 chars is the bound `GET /api/disputes/{id}` already uses. An id longer
    # than any the store can mint is refused at the edge rather than turned
    # into a lookup, so an adjudication route cannot be used as a store probe.
    calls_uphold = upholds_with(monkeypatch, credited())
    calls_reject = rejects_with(monkeypatch, record(status="rejected"))
    oversized = path.replace(DISPUTE_ID, "d" * 65)

    r = client.post(oversized, json={}, headers=AUTH)

    assert r.status_code == 422
    assert calls_uphold == []
    assert calls_reject == []
