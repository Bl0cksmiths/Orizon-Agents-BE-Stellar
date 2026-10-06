"""The lifecycle harness pinned against the backend it drives (story 5.01).

The harness imports nothing from `app/` at runtime — it talks HTTP, as a buyer
would. The few places where it re-states a server-side fact are pinned here
instead, so a backend change that would make the harness sign the wrong bytes,
read the wrong route or miss a rating fails in CI rather than on testnet.
"""

from __future__ import annotations

import base64
import inspect
from typing import Any

import pytest
from stellar_sdk import Keypair

import app.stellar.client as sc
from app.main import app
from app.services import execution_svc
from app.services import external_binding as eb
from scripts.lifecycle import signing
from scripts.lifecycle.api import API_KEY_HEADER, DISPUTE_READ_GRANT_HEADER, TASK_TOKEN_HEADER
from scripts.lifecycle.signing import SigningRefused, check_dispute_message, check_read_message
from scripts.lifecycle.stages import RATED_LINE, UNLANDED_LINE

JOB = "0a" * 16


def _schema() -> dict[str, Any]:
    return app.openapi()


def _body_fields(path: str) -> set[str]:
    schema = _schema()
    ref = schema["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    return set(schema["components"]["schemas"][ref.split("/")[-1]]["properties"])


# ── the routes and bodies the harness sends ─────────────────────
@pytest.mark.parametrize(
    ("path", "fields"),
    [
        # `spec` is the optional corrected reading a console resubmits; the
        # harness plans from an intent alone and never sends one.
        ("/api/orchestrator/decompose", {"intent", "spec"}),
        # `max_amount_stroops` is the exact amount (ADR 0015); the harness still
        # sends the legacy float, which converts by the same rule.
        ("/api/stellar/build/authorize", {"payer", "agent_id", "max_amount_stroops", "max_amount_usdc", "ttl_seconds"}),
        ("/api/stellar/submit", {"signed_xdr"}),
        ("/api/orchestrator/execute", {"plan_id", "auth_id_hex", "payer"}),
        ("/api/disputes/challenge", {"job_id_hex", "step_index"}),
        ("/api/disputes", {"job_id_hex", "step_index", "reason", "payer", "nonce", "signature_b64"}),
        ("/api/disputes/read-challenge", {"task_id"}),
        ("/api/disputes/read-grant", {"task_id", "nonce", "signature_b64"}),
    ],
)
def test_every_body_the_harness_posts_is_the_routes_own_shape(path: str, fields: set[str]) -> None:
    assert _body_fields(path) == fields


def test_every_route_the_harness_reads_exists() -> None:
    paths = _schema()["paths"]
    for path, method in [
        ("/api/health", "get"),
        ("/api/stellar/network", "get"),
        ("/api/agents", "get"),
        ("/api/stellar/agent/{agent_id}", "get"),
        ("/api/stellar/reputation", "get"),
        ("/api/tasks/{task_id}", "get"),
        ("/api/trace/{task_id}", "get"),
        ("/api/tasks/{task_id}/disputes", "get"),
        ("/api/disputes/{dispute_id}", "get"),
        ("/api/disputes/{dispute_id}/uphold", "post"),
        ("/readiness", "get"),
    ]:
        assert method in paths[path], path


def test_the_headers_the_harness_sends_are_the_ones_the_routes_read() -> None:
    paths = _schema()["paths"]
    task_params = {p["name"] for p in paths["/api/tasks/{task_id}"]["get"]["parameters"]}
    assert TASK_TOKEN_HEADER in task_params
    # The uphold's key is declared as a security scheme on main (older builds
    # listed it as a parameter); either way it must be the X-API-Key header.
    uphold = paths["/api/disputes/{dispute_id}/uphold"]["post"]
    schemes = _schema()["components"].get("securitySchemes", {})
    declared = {p["name"] for p in uphold.get("parameters", [])} | {
        schemes[name]["name"] for req in uphold.get("security", []) for name in req if schemes[name]["in"] == "header"
    }
    assert API_KEY_HEADER in declared
    dispute_params = {p["name"] for p in paths["/api/disputes/{dispute_id}"]["get"]["parameters"]}
    assert DISPUTE_READ_GRANT_HEADER in dispute_params


# ── the messages the harness checks before it signs ─────────────
def test_the_dispute_message_the_server_builds_passes_the_harness_check() -> None:
    nonce = "ab" * 16
    check_dispute_message(eb.dispute_message(JOB, 3, nonce), JOB, 3, nonce)


def test_a_message_for_another_step_is_refused() -> None:
    nonce = "ab" * 16
    with pytest.raises(SigningRefused):
        check_dispute_message(eb.dispute_message(JOB, 4, nonce), JOB, 3, nonce)
    with pytest.raises(SigningRefused):
        check_dispute_message(eb.dispute_message("0b" * 16, 3, nonce), JOB, 3, nonce)
    with pytest.raises(SigningRefused):
        check_dispute_message(eb.dispute_message(JOB, 3, nonce), JOB, 3, "cd" * 16)


def test_the_read_message_the_server_builds_passes_the_harness_check() -> None:
    nonce = "ef" * 16
    check_read_message(eb.dispute_read_message("tsk_abc123", nonce), "tsk_abc123", nonce)
    with pytest.raises(SigningRefused):
        check_read_message(eb.dispute_read_message("tsk_other", nonce), "tsk_abc123", nonce)


def test_the_harness_signature_verifies_as_the_backend_verifies_it() -> None:
    kp = Keypair.random()
    message = eb.dispute_message(JOB, 0, "12" * 16)
    signature = signing.sign_message_b64(kp, message)
    assert eb._signature_matches(kp.public_key, message, signature)
    # and it is SEP-53, the wallet's encoding — not a raw-bytes signature
    raw = base64.b64decode(signature)
    with pytest.raises(ValueError):
        Keypair.from_public_key(kp.public_key).verify(message.encode(), raw)


# ── the stroop conversion and the trace lines ───────────────────
@pytest.mark.parametrize("amount", [0.001, 0.012, 0.05, 0.07, 1.2345678, 12.5, 0.00000125, 0.00000455])
def test_stroops_are_the_backends_stroops(amount: float) -> None:
    assert signing.usdc_to_stroops(amount) == sc.usdc_to_i128(amount)


def test_the_rating_trace_lines_are_the_ones_execution_writes() -> None:
    source = inspect.getsource(execution_svc)
    assert 'f"reputation → {step.agent_name} rated {rating}/100 · tx {tx[:10]}…"' in source
    assert 'f"{rating_writer.unlanded_reason(status)} · tx {tx[:10]}…"' in source
    tx = "1234567890abcdef"
    landed = f"reputation → Code Smith rated 90/100 · tx {tx[:10]}…"
    m = RATED_LINE.match(landed)
    assert m and m.group("name") == "Code Smith" and m.group("prefix") == tx[:10]
    unlanded = f"reputation submit failed for Code Smith: did not land · tx {tx[:10]}…"
    u = UNLANDED_LINE.match(unlanded)
    assert u and u.group("name") == "Code Smith" and u.group("prefix") == tx[:10]
