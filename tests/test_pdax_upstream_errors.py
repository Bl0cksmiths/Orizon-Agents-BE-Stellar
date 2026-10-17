"""Malformed upstream input must become a typed PdaxError, never a 500.

PdaxError is what `with_retries` retries on and what every route translates
(`_fail` → 502 upstream_unavailable). Anything else escapes both, so a
gateway HTML page from PDAX would read to the client as an Orizon bug.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.pdax import auth as auth_mod
from app.pdax import balances as pb
from app.pdax import transactions as ptx
from app.pdax import withdrawals as pwd
from app.pdax.auth import PdaxAuth, _send
from app.pdax.errors import PdaxError, orizon_code
from app.pdax.models.withdrawals import CryptoOutRequest, FiatWithdrawRequest
from app.pdax.resilience import is_retryable
from app.pdax.totp import totp_now


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://pdax.test/")


def _send_once(handler) -> dict:
    async def run() -> dict:
        async with _client(handler) as http:
            return await _send(http, "POST", "login", {"username": "u"})

    return asyncio.run(run())


def test_html_gateway_page_becomes_typed_upstream_error():
    """A Cloudflare/WAF error page is not JSON — resp.json() raises ValueError
    inside _send, which used to escape as 500 internal_error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html><body>502 Bad Gateway</body></html>")

    with pytest.raises(PdaxError) as exc:
        _send_once(handler)
    e = exc.value
    assert e.http_status == 502
    assert "502 Bad Gateway" in e.message
    # 502 is transient, so the transport may legitimately retry it...
    assert is_retryable(e) is True
    # ...and the client sees an upstream outage, not an app fault.
    assert orizon_code(e.code) == "pdax_error"


def test_json_array_error_body_becomes_typed_upstream_error():
    """Valid JSON, wrong shape: a list has no .get, which used to raise
    AttributeError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=[{"code": "InvalidCredentials"}])

    with pytest.raises(PdaxError) as exc:
        _send_once(handler)
    assert exc.value.http_status == 400
    assert "PDAX authentication failed" in exc.value.message


def test_json_array_success_body_becomes_typed_upstream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "token", "set"])

    with pytest.raises(PdaxError) as exc:
        _send_once(handler)
    assert exc.value.code == "bad_upstream_shape"


def test_non_json_success_body_becomes_typed_upstream_error(monkeypatch):
    """A 200 carrying an HTML maintenance page: _send salvages it as
    {"message": ...} (like client._parse), and the token-set guard rejects it
    as a typed upstream error instead of a ValidationError 500."""
    monkeypatch.setattr(auth_mod.settings, "pdax_username", "u")
    monkeypatch.setattr(auth_mod.settings, "pdax_password", "p")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    async def run() -> None:
        async with _client(handler) as http:
            await PdaxAuth().access_headers(http)

    login = run()
    with pytest.raises(PdaxError) as exc:
        asyncio.run(login)
    assert exc.value.code == "bad_upstream_shape"


def test_typed_error_body_still_surfaces_its_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "InvalidCredentials", "message": "Incorrect username or password."})

    with pytest.raises(PdaxError) as exc:
        _send_once(handler)
    assert exc.value.code == "InvalidCredentials"
    assert orizon_code(exc.value.code) == "invalid_credentials"


def test_ok_body_is_returned_unchanged():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"challenge_name": "SOFTWARE_TOKEN_MFA", "session": "s1"})

    assert _send_once(handler)["session"] == "s1"


def test_token_shaped_2xx_missing_fields_is_typed(monkeypatch):
    """A 200 that is a JSON object but not a token set must not escape as a
    pydantic ValidationError 500."""
    monkeypatch.setattr(auth_mod.settings, "pdax_username", "u")
    monkeypatch.setattr(auth_mod.settings, "pdax_password", "p")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    async def run() -> None:
        async with _client(handler) as http:
            await PdaxAuth().access_headers(http)

    login = run()
    with pytest.raises(PdaxError) as exc:
        asyncio.run(login)
    assert exc.value.code == "bad_upstream_shape"


def test_mfa_challenge_without_session_is_typed(monkeypatch):
    monkeypatch.setattr(auth_mod.settings, "pdax_username", "u")
    monkeypatch.setattr(auth_mod.settings, "pdax_password", "p")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"challenge_name": "SOFTWARE_TOKEN_MFA"})

    async def run() -> None:
        async with _client(handler) as http:
            await PdaxAuth().access_headers(http)

    login = run()
    with pytest.raises(PdaxError) as exc:
        asyncio.run(login)
    assert exc.value.code == "InvalidMfaCode"


def test_malformed_otp_secret_is_typed_not_binascii_error():
    """A mistyped PDAX_OTP_SECRET used to raise binascii.Error on the first
    MFA login — an unhandled 500 rather than a PDAX auth failure."""
    with pytest.raises(PdaxError) as exc:
        totp_now("not-valid-base32!", timestamp=59)
    assert exc.value.code == "InvalidMfaCode"
    assert orizon_code(exc.value.code) == "invalid_mfa_code"


def test_empty_otp_secret_is_typed():
    with pytest.raises(PdaxError):
        totp_now("A", timestamp=59)


# ── response parsers ────────────────────────────────────────────
# The withdrawal, transaction, and balance parsers used to build their
# pydantic models straight off the response body. A ValidationError (or the
# AttributeError a JSON-array body raises on `.get`) is not a PdaxError, so it
# escaped every route's handler as an opaque 500 instead of the curated
# upstream envelope. They now share trade._unwrap / _unwrap_list.


class _StubClient:
    """Stands in for PdaxClient, replaying one canned response body."""

    def __init__(self, body):
        self.body = body

    async def request(self, method, path, **kwargs):
        return self.body


def _parse(parser, body):
    """Drive one response parser against a canned 2xx upstream body."""
    return asyncio.run(parser(_StubClient(body)))


_WITHDRAW_REQ = FiatWithdrawRequest(
    identifier="wd-1",
    amount="100",
    method="PAY-TO-ACCOUNT-NON-REAL-TIME",
    fee_type="Sender",
    sender_first_name="Ada",
    sender_last_name="Lovelace",
    sender_country_origin="Philippines",
    source_of_funds="Compensation",
    beneficiary_first_name="Ada",
    beneficiary_last_name="Lovelace",
    beneficiary_bank_code="BABDOPH",
    beneficiary_account_name="Ada Lovelace",
    beneficiary_account_number="1234567890",
    purpose="Purchase of Goods",
    relationship_of_sender_to_beneficiary="Myself",
)
_CRYPTO_OUT_REQ = CryptoOutRequest(identifier="co-1", currency="USDCXLM", amount="5", address="GRECIPIENT")

_FIAT_RESULT = {"identifier": "wd-1", "amount": 100.0, "method": "PAY-TO-ACCOUNT-NON-REAL-TIME"}
_CRYPTO_OUT_RESULT = {
    "identifier": "co-1",
    "transaction_id": 7,
    "amount": "5",
    "address": "GRECIPIENT",
    "total": "5",
    "fee": "0",
    "currency": "USDCXLM",
}

# (parser, a 2xx body the model rejects, a 2xx body it accepts) — one entry
# per call site that used to build its model without the unwrap.
_PARSERS = [
    pytest.param(
        lambda c: pwd.fiat_withdraw(c, _WITHDRAW_REQ),
        {"data": {**_FIAT_RESULT, "amount": "not-a-number"}},
        {"data": _FIAT_RESULT},
        id="fiat_withdraw",
    ),
    pytest.param(
        lambda c: pwd.user_info_upload(c, _WITHDRAW_REQ),
        {"identifier": "wd-1"},  # no amount, no method
        _FIAT_RESULT,
        id="user_info_upload",
    ),
    pytest.param(
        lambda c: pwd.crypto_out(c, _CRYPTO_OUT_REQ),
        {**_CRYPTO_OUT_RESULT, "transaction_id": "not-an-int"},
        _CRYPTO_OUT_RESULT,
        id="crypto_out",
    ),
    pytest.param(
        lambda c: ptx.fiat_transactions(c),
        {"data": [{"request_id": "r-1"}]},  # missing every other required field
        {"data": []},
        id="fiat_transactions",
    ),
    pytest.param(
        lambda c: ptx.crypto_transactions(c),
        {"data": [{"transaction_id": "not-an-int", "type": "crypto_in", "status": "completed"}]},
        {"data": []},
        id="crypto_transactions",
    ),
    pytest.param(
        lambda c: pb.get_balances(c),
        {"data": [{"currency": "PHP"}]},  # missing available/total/asset_type
        {"data": [{"currency": "PHP", "available": "10", "total": "10", "asset_type": "FIAT"}]},
        id="get_balances",
    ),
]


@pytest.mark.parametrize("parser, bad, ok", _PARSERS)
def test_malformed_2xx_body_becomes_typed_upstream_error(parser, bad, ok):
    """A 2xx the model rejects is an upstream contract break, not an app
    fault: it must arrive as a PdaxError the routes already translate."""
    with pytest.raises(PdaxError) as exc:
        _parse(parser, bad)
    assert exc.value.code == "bad_upstream_shape"


@pytest.mark.parametrize("parser, bad, ok", _PARSERS)
def test_json_array_2xx_body_becomes_typed_upstream_error(parser, bad, ok):
    """Valid JSON, wrong shape: a list has no .get, which used to raise
    AttributeError straight out of the parser."""
    with pytest.raises(PdaxError) as exc:
        _parse(parser, [{"identifier": "wd-1"}])
    assert exc.value.code == "bad_upstream_shape"


@pytest.mark.parametrize("parser, bad, ok", _PARSERS)
def test_wellformed_2xx_body_still_parses(parser, bad, ok):
    """The unwrap must not swallow the happy path."""
    assert _parse(parser, ok) is not None


def test_crypto_out_curated_envelope_not_500(client, monkeypatch):
    """Route level: the crypto-out parser is the ramp's off-ramp payout leg.
    A malformed body there must surface as the curated upstream envelope."""
    monkeypatch.setattr(
        "app.routers.pdax.get_pdax_client",
        lambda: _StubClient({"identifier": "co-1", "transaction_id": "not-an-int"}),
    )
    r = client.post(
        "/api/pdax/crypto/withdraw",
        json={"identifier": "co-1", "currency": "USDCXLM", "amount": "5", "address": "GRECIPIENT"},
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "upstream_unavailable"


def test_balances_json_array_body_curated_envelope_not_500(client, monkeypatch):
    monkeypatch.setattr("app.routers.pdax.get_pdax_client", lambda: _StubClient(["PHP", "USDCXLM"]))
    r = client.get("/api/pdax/balances")
    assert r.status_code == 502
    assert r.json()["detail"] == "upstream_unavailable"
