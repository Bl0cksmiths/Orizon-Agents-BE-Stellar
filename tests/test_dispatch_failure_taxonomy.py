"""The dispatch failure taxonomy — story 2.03, AC-3 (ADR 0005).

Twelve distinct ways a dispatch can fail used to reach the buyer as one trace
line, `f"{worker.name} failed"`, and reach the log as prose — distinguishable
by a human reading one line, useless for grouping a week of them and impossible
to assert on. `ExternalDispatchError.rule` is the machine-readable half, and
these tests pin it end to end: every rule is produced by driving the REAL
worker through the REAL `_dispatch` path against an `httpx.MockTransport`, so a
rule that no longer fires is a failing test rather than a dead error code.

Hermetic: MockTransport answers in-process and opens no socket. The endpoint
URLs below are labels — except where the URL itself is the thing under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, NamedTuple

import httpx
import pytest

from app.agents.workers import external_http as eh

# A credential in the query string, which is an ordinary way to write an
# operator endpoint. `execution_svc` logs an ExternalDispatchError's message
# verbatim, so the day this string turns up in one of those messages is the day
# we start writing operator bearer tokens into our own server log.
SECRET = "s3cr3t-bearer-token"
ENDPOINT = f"https://operator.example/dispatch?token={SECRET}"
# Refused by the SSRF policy, and carrying the same credential: the refusal
# path is the one that used to pass the policy's `{url!r}` prose straight
# through.
BLOCKED_ENDPOINT = f"https://169.254.169.254/latest/meta-data/?token={SECRET}"

Handler = Callable[[httpx.Request], Any]


def _worker(handler: Handler, endpoint: str = ENDPOINT) -> eh.ExternalHttpWorker:
    return eh.ExternalHttpWorker(
        "ext_demo1", "external.demo", endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _failure(handler: Handler, endpoint: str = ENDPOINT) -> eh.ExternalDispatchError:
    """Drive one real dispatch and hand back the error it failed with."""
    with pytest.raises(eh.ExternalDispatchError) as caught:
        asyncio.run(_worker(handler, endpoint).run("build it", "because"))
    return caught.value


@pytest.fixture()
def short_deadline(monkeypatch: pytest.MonkeyPatch) -> float:
    """A 1 s budget so the suite stays fast; the mechanism is identical."""
    monkeypatch.setattr(eh, "DISPATCH_DEADLINE_SECONDS", 1.0)
    return 1.0


# --- handlers, one per failure mode -----------------------------------------


def _answers(payload: object, status: int = 200) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def _raising(exc: type[Exception], *args: object) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc(*args)

    return handler


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"summary": "done"})


def _never_answers(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("the endpoint must not be contacted")


async def _slow(_request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(30)  # far past any deadline this suite sets
    return httpx.Response(200, json={"summary": "too late"})


async def _breaks_mid_body(_request: httpx.Request) -> httpx.Response:
    async def gen() -> Any:
        yield b'{"summ'
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    return httpx.Response(200, content=gen())


def _not_json(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"this is not json", headers={"Content-Type": "application/json"})


def _too_deep(_request: httpx.Request) -> httpx.Response:
    # Valid JSON, well under the size cap, and ~20x CPython's recursion limit:
    # the scanner raises RecursionError rather than returning a document.
    return httpx.Response(200, content=b"[" * 20_000 + b"]" * 20_000, headers={"Content-Type": "application/json"})


def _oversize(_request: httpx.Request) -> httpx.Response:
    # The real cap, not a patched one — the body is cut off before it is
    # buffered, so nothing here depends on how big MAX_RESPONSE_BYTES is today.
    return httpx.Response(200, content=b"x" * (eh.MAX_RESPONSE_BYTES + 1024))


# --- the table: every rule in the closed vocabulary, and what produces it ----


class Case(NamedTuple):
    name: str
    handler: Handler
    rule: str
    endpoint: str = ENDPOINT


CASES: tuple[Case, ...] = (
    # The URL never passed the SSRF policy, so nothing was dispatched.
    Case("blocked_endpoint", _never_answers, "endpoint_refused", BLOCKED_ENDPOINT),
    # Nothing connected, retry included.
    Case("connect_error", _raising(httpx.ConnectError, "connection refused"), "no_connection"),
    Case("connect_timeout", _raising(httpx.ConnectTimeout, "timed out connecting"), "no_connection"),
    # Time ran out with the request already committed.
    Case("slow_operator", _slow, "response_timeout"),
    Case("read_timeout", _raising(httpx.ReadTimeout, "read timed out"), "response_timeout"),
    Case("write_timeout", _raising(httpx.WriteTimeout, "write timed out"), "response_timeout"),
    Case("pool_timeout", _raising(httpx.PoolTimeout, "no connection available"), "response_timeout"),
    # The HTTP conversation itself broke.
    Case("remote_protocol_error", _raising(httpx.RemoteProtocolError, "malformed HTTP"), "transport_error"),
    Case("decoding_error", _raising(httpx.DecodingError, "bad content-encoding"), "transport_error"),
    Case("read_error", _raising(httpx.ReadError, "connection reset"), "transport_error"),
    Case("write_error", _raising(httpx.WriteError, "broken pipe"), "transport_error"),
    Case("proxy_error", _raising(httpx.ProxyError, "proxy refused"), "transport_error"),
    Case("local_protocol_error", _raising(httpx.LocalProtocolError, "illegal header"), "transport_error"),
    Case("broken_mid_body", _breaks_mid_body, "transport_error"),
    # The operator answered, with something that is not a usable 2xx.
    Case("server_error", _answers({"error": "boom"}, status=500), "error_status"),
    Case("redirect", _answers({"go": "elsewhere"}, status=302), "error_status"),
    Case("too_many_redirects", _raising(httpx.TooManyRedirects, "exceeded maximum redirects"), "error_status"),
    # The body was too big to read.
    Case("oversize_body", _oversize, "oversize_response"),
    # The body arrived whole and is not the documented shape.
    Case("not_json", _not_json, "invalid_response"),
    Case("nesting_too_deep", _too_deep, "invalid_response"),
    Case("not_an_object", _answers([1, 2, 3]), "invalid_response"),
    Case("no_summary", _answers({"artifact": {"title": "x", "files": []}}), "invalid_response"),
    Case("hostile_artifact", _answers({"summary": "done", "artifact": "boom"}), "invalid_response"),
)

_IDS = [c.name for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_each_failure_mode_names_its_class(case: Case, short_deadline: float) -> None:
    error = _failure(case.handler, case.endpoint)
    assert error.rule == case.rule
    assert error.rule in eh.DISPATCH_RULES


@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_no_failure_message_carries_the_endpoint_url(case: Case, short_deadline: float) -> None:
    # The regression test for the log leak. Every one of these messages is
    # logged verbatim upstream, so none of them may quote the endpoint — and a
    # token in the query string is the part that actually costs something.
    message = str(_failure(case.handler, case.endpoint))
    assert SECRET not in message
    assert case.endpoint not in message
    assert "token=" not in message


def test_every_rule_in_the_vocabulary_is_reachable() -> None:
    # The vocabulary is closed; this is what keeps it from also being stale. A
    # rule nothing can raise is a class an operator would write a runbook entry
    # for and never see, and a rule missing from the table is one nothing above
    # proves is produced by the real worker.
    assert {case.rule for case in CASES} == eh.DISPATCH_RULES


# --- AC-3: the three named classes are three different classes --------------


def test_the_three_ac3_classes_are_distinguishable(short_deadline: float) -> None:
    # The acceptance criterion, stated as code: a timeout, a refused connection
    # and a rejected schema each name their OWN class. Before the rule they
    # were byte-identical to a buyer — all three rendered as "external.demo
    # failed" — so this is the assertion the story turns on.
    timeout = _failure(_slow)
    refused = _failure(_raising(httpx.ConnectError, "connection refused"))
    schema = _failure(_answers({"summary": "done", "artifact": "boom"}))

    assert len({timeout.rule, refused.rule, schema.rule}) == 3
    assert timeout.rule == "response_timeout"
    assert refused.rule == "no_connection"
    assert schema.rule == "invalid_response"


def test_a_spent_deadline_is_a_timeout_not_a_new_class(monkeypatch: pytest.MonkeyPatch) -> None:
    # The budget can also run out BEFORE an attempt starts — the retry loop
    # re-checks it. Same remedy for the operator (answer faster), so the same
    # class: a second timeout token would be a distinction nobody could act on.
    monkeypatch.setattr(eh, "DISPATCH_DEADLINE_SECONDS", 0.0)
    error = _failure(_never_answers)
    assert error.rule == "response_timeout"
    assert "deadline" in str(error)


def test_a_timeout_is_never_retried(short_deadline: float) -> None:
    # "No retry on a step that may have executed": a read timeout means the
    # request reached the operator, so a retry risks running the step twice.
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("read timed out")

    assert _failure(handler).rule == "response_timeout"
    assert calls["n"] == 1


def test_a_broken_transport_is_never_retried(short_deadline: float) -> None:
    # Same reasoning one layer along: a RemoteProtocolError follows a request
    # that was fully sent, so the step may have run to completion already.
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.RemoteProtocolError("malformed HTTP")

    assert _failure(handler).rule == "transport_error"
    assert calls["n"] == 1


def test_only_a_failed_connection_is_retried(short_deadline: float) -> None:
    # The one case where a retry is safe: the operator never received the step.
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    assert _failure(handler).rule == "no_connection"
    assert calls["n"] == 2  # the attempt plus exactly one retry


# --- nothing httpx can raise escapes the module -----------------------------


@pytest.mark.parametrize(
    "exc",
    [
        # The seven the ADR names: none of these was caught before story 2.03,
        # so each one left the module as a raw httpx error that execution_svc
        # could not classify and no per-agent failure count could see.
        httpx.RemoteProtocolError,
        httpx.DecodingError,
        httpx.ReadTimeout,
        httpx.ReadError,
        httpx.PoolTimeout,
        httpx.WriteTimeout,
        httpx.TooManyRedirects,
        # …and the rest of the transport family, so the docstring's "any
        # failure" is a closed claim rather than a list that drifts.
        httpx.WriteError,
        httpx.CloseError,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ProxyError,
        httpx.UnsupportedProtocol,
        httpx.LocalProtocolError,
        httpx.ProtocolError,
        httpx.NetworkError,
        httpx.TransportError,
        httpx.TimeoutException,
        httpx.RequestError,
    ],
    ids=lambda e: e.__name__,
)
def test_no_httpx_exception_escapes_the_worker(exc: type[Exception], short_deadline: float) -> None:
    error = _failure(_raising(exc, "boom"))
    # The type is the contract: execution_svc's step handler catches this and
    # skips the step. A raw httpx error would reach the run-level handler
    # instead and fail the whole workflow.
    assert type(error) is eh.ExternalDispatchError
    assert error.rule in eh.DISPATCH_RULES
    # The httpx type survives in the prose — the coarse class groups the log,
    # the type name is what a human needs to debug one dispatch.
    assert exc.__name__ in str(error)


# --- the constructor's closed vocabulary ------------------------------------


def test_an_unknown_rule_is_refused() -> None:
    # A typo at a raise site must fail loudly HERE rather than travelling to
    # the trace as a class nothing handles. "timeout" is the plausible typo:
    # a synonym of a real rule, which is exactly what a closed set is for.
    with pytest.raises(ValueError) as caught:
        eh.ExternalDispatchError("timeout", "no response in time")
    assert "unknown dispatch rule" in str(caught.value)
    # A plain ValueError, not the dispatch error itself — a caller must not be
    # able to catch the typo with the same clause it catches a failed step.
    assert not isinstance(caught.value, eh.ExternalDispatchError)


def test_every_rule_in_the_vocabulary_is_constructible() -> None:
    for rule in eh.DISPATCH_RULES:
        assert eh.ExternalDispatchError(rule, "message").rule == rule


# --- wrapped vocabularies keep their own rule -------------------------------


def test_a_wrapped_contract_refusal_keeps_the_contract_rule() -> None:
    # The coarse class goes to the trace; the specific rule stays useful in the
    # log. `artifact_not_an_object` is what tells an operator WHICH part of
    # their response the contract refused — invalid_response alone does not.
    error = _failure(_answers({"summary": "done", "artifact": "boom"}))
    assert error.rule == "invalid_response"
    assert "artifact_not_an_object" in str(error)


def test_a_wrapped_policy_refusal_keeps_the_policy_rule() -> None:
    error = _failure(_never_answers, BLOCKED_ENDPOINT)
    assert error.rule == "endpoint_refused"
    assert "non_public_address" in str(error)


def test_a_policy_refusal_names_the_host_but_not_the_url() -> None:
    # ADR 0003's logging rule, now followed on the dispatch path too: host and
    # rule, never the attacker-controlled URL. The policy's own message embeds
    # `{url!r}` — right for the bind API, which answers the operator who typed
    # it, and wrong here, where the message lands in our server log.
    error = _failure(_never_answers, BLOCKED_ENDPOINT)
    message = str(error)
    assert "169.254.169.254" in message  # the host is still there to act on
    assert SECRET not in message
    assert BLOCKED_ENDPOINT not in message
    assert "/latest/meta-data/" not in message  # nor the path it was fishing in
    # and the prose that explains the refusal survives the redaction
    assert "non-public address" in message


def test_a_refused_endpoint_is_never_contacted() -> None:
    # `_never_answers` asserts if it is called, so this passing at all is the
    # proof: the policy runs before the transport, and the credential in the
    # URL was never put on a wire either.
    assert _failure(_never_answers, BLOCKED_ENDPOINT).rule == "endpoint_refused"


# --- the happy path still works ---------------------------------------------


def test_a_good_response_still_returns_output() -> None:
    # The taxonomy is about failures; this is the guard that none of it fires
    # on a dispatch that worked.
    assert asyncio.run(_worker(_ok).run("x", "y"))["summary"] == "done"
