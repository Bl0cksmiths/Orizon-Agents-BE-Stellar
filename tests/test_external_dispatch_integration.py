"""Where the dispatch worker meets the output contract — story 2.02.

`tests/test_external_contract.py` proves the contract in isolation and
`tests/test_dispatch_signing.py` proves the signature in isolation. What is
only observable HERE is the seam: that a hostile response is turned into a
STEP failure rather than a run failure, that nothing an operator chose
survives into the returned dict, and that the signature covers the exact bytes
that go on the wire.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import httpx
import pytest
from stellar_sdk import Keypair

from app.agents.workers import external_http as eh
from app.services import dispatch_signing as ds

ENDPOINT = "https://operator.example/run"


def _run(handler) -> dict:
    worker = eh.ExternalHttpWorker(
        "ext_demo1", "external.ext_demo1", ENDPOINT, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return asyncio.run(worker.run("build it", "because"))


def _responds(payload: object):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


def test_an_operator_cannot_claim_provenance() -> None:
    # synthetic_rating awards 95/100 for source == "baked". The operator sends
    # it; what comes back is OUR stamp, so the self-award never happens.
    out = _run(_responds({"summary": "done", "source": "baked"}))
    assert out["source"] == "external"


def test_operator_chosen_keys_never_survive() -> None:
    # Anything not on the allowlist would otherwise land in context, be
    # forwarded to every later operator, and reach the buyer's artifact view.
    out = _run(_responds({"summary": "done", "validator_violations": [], "sneaky": {"a": 1}}))
    assert "sneaky" not in out
    # validator_violations is a rating lever: an empty one is worth +10.
    assert "validator_violations" not in out


def test_a_hostile_artifact_shape_fails_the_step_not_the_run() -> None:
    with pytest.raises(eh.ExternalDispatchError, match="rule: artifact_not_an_object"):
        _run(_responds({"summary": "done", "artifact": "boom"}))


def test_deeply_nested_json_fails_the_step_not_the_run() -> None:
    # ~500k-deep nesting blows CPython's recursive scanner. RecursionError is a
    # RuntimeError subclass, so without an explicit catch it escapes as a RUN
    # failure and takes everyone else's settlement with it.
    deep = b"[" * 200_000 + b"]" * 200_000

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=deep, headers={"Content-Type": "application/json"})

    worker = eh.ExternalHttpWorker(
        "ext_demo1", "external.ext_demo1", ENDPOINT, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(eh.ExternalDispatchError):
        asyncio.run(worker.run("x", "y"))


def test_the_signature_covers_the_exact_bytes_sent(hermetic_settings) -> None:
    # The trap this pins: httpx's json= encodes with separators=(",",":") and
    # ensure_ascii=False, which differs from json.dumps() defaults. Signing a
    # differently-serialized copy would verify in every ASCII test and fail in
    # production on the first accented character — so the intent is non-ASCII.
    kp = Keypair.random()
    hermetic_settings.orizon_dispatch_signing_key = kp.secret
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["sig"] = request.headers.get(ds.SIGNATURE_HEADER)
        return httpx.Response(200, json={"summary": "ok"})

    worker = eh.ExternalHttpWorker(
        "ext_demo1", "external.ext_demo1", ENDPOINT, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    asyncio.run(worker.run("café — naïve", "駆動"))

    message = ds.dispatch_message(ENDPOINT, seen["body"])
    Keypair.from_public_key(kp.public_key).verify_message(message.encode(), base64.b64decode(seen["sig"]))

    # and the digest in the message really is of the bytes that were sent
    assert hashlib.sha256(seen["body"]).hexdigest() in message
    assert json.loads(seen["body"])["intent"] == "café — naïve"
