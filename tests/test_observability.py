"""Log-correlation observability: records emitted while a request is in
flight carry the request id injected by RequestIdLogFilter, and the access
line includes the resolved client key.

Also covers the Soroban RPC instrumentation in app/stellar/client.py — the
timing/outcome lines and, critically, their LEVELS: reads are high-volume
(twelve per metrics poll) so successes must stay off INFO, while slow and
failed calls must be loud.
"""

from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.stellar import client as sc

RPC_LOGGER = "app.stellar.client"


def test_log_record_during_request_carries_request_id(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.security"):
        r = client.get("/api/agents", headers={"X-Request-ID": "caplog-rid-7"})
    assert r.status_code == 200

    access = [rec for rec in caplog.records if rec.name == "app.security" and "/api/agents" in rec.getMessage()]
    assert access, "expected an access log record for the request"
    # RequestIdLogFilter stamps the id from the request's context onto the
    # record, so service logs correlate with the X-Request-ID echoed to the
    # client.
    assert all(getattr(rec, "request_id", "-") == "caplog-rid-7" for rec in access)
    # The access line names the same client key the rate limiter buckets on.
    assert "client=" in access[-1].getMessage()


def test_the_access_record_carries_its_facts_as_structured_fields(client, caplog):
    # A log platform filters on fields, not on a sentence: status, route and
    # duration are emitted as data beside the human line, and the token in the
    # query string is masked there exactly as it is in the line.
    with caplog.at_level(logging.INFO, logger="app.security"):
        client.get("/api/agents?task_token=sekrit&x=1", headers={"X-Request-ID": "fields-rid-1"})

    access = [rec for rec in caplog.records if rec.name == "app.security" and "/api/agents" in rec.getMessage()]
    http = access[-1].http
    assert http["method"] == "GET"
    assert http["target"] == "/api/agents?task_token=***&x=1"
    assert http["status"] == 200
    assert isinstance(http["duration_ms"], float)
    assert http["client"]
    # The key the rate limiters used, so a 429 is traceable to its bucket.
    assert http["identity"] == "testclient"
    assert "sekrit" not in repr(http)


def test_the_json_formatter_emits_structured_fields():
    import json

    from app.main import JsonLogFormatter

    record = logging.LogRecord("app.security", logging.INFO, __file__, 1, "GET / -> 200", None, None)
    record.http = {"method": "GET", "status": 200}
    record.request_id = "rid-9"

    line = json.loads(JsonLogFormatter().format(record))

    assert line["http"] == {"method": "GET", "status": 200}
    assert line["request_id"] == "rid-9"
    # And a record without fields keeps the original five keys only.
    plain = logging.LogRecord("app", logging.INFO, __file__, 1, "hello", None, None)
    assert set(json.loads(JsonLogFormatter().format(plain))) == {"ts", "level", "logger", "msg", "request_id"}


# ── Soroban RPC instrumentation ────────────────────────────────────────
class _FakeSimulation:
    error = None
    results: list = []


class _FakeServer:
    """Stand-in for SorobanServer — no sockets, no network."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on

    def load_account(self, address):
        if self.fail_on == "load_account":
            raise RuntimeError("429 too many requests")
        from stellar_sdk import Account

        return Account(address, 1)

    def simulate_transaction(self, tx):
        if self.fail_on == "simulate":
            raise RuntimeError("rpc unreachable")
        return _FakeSimulation()


# Public testnet AgentRegistry id. These reads build a real simulation
# envelope, so contract_ids() must resolve to a well-formed id — the settings
# default is "" and only a gitignored .env ever filled it, which is why this
# file passed locally and failed in CI with "contract_id is invalid".
_TEST_AGENT_REGISTRY = "CAPHXWU53UZUZJGV7IAE57NNMH3YYB5MTWO6YA53KKMXSFVLOITBJ3GQ"


@pytest.fixture()
def fake_rpc(monkeypatch):
    """Point client._server() at a scriptable in-process fake."""
    # contract_ids() is lru_cached, so the id has to be in place before the
    # first call and the cache dropped on both sides of the test.
    monkeypatch.setattr(settings, "stellar_agent_registry", _TEST_AGENT_REGISTRY)
    sc.contract_ids.cache_clear()

    def install(fail_on: str | None = None) -> _FakeServer:
        server = _FakeServer(fail_on)
        monkeypatch.setattr(sc, "_server", lambda **_kw: server)
        return server

    yield install
    sc.contract_ids.cache_clear()


def _rpc_lines(caplog, level: int) -> list[str]:
    return [rec.getMessage() for rec in caplog.records if rec.name == RPC_LOGGER and rec.levelno == level]


def test_simulate_read_logs_one_timed_line_per_call(fake_rpc, caplog):
    """A successful read emits exactly one aggregated line carrying the
    contract, the function, and BOTH round-trips' latencies."""
    fake_rpc()
    with caplog.at_level(logging.DEBUG, logger=RPC_LOGGER):
        assert sc.simulate_read(sc.contract_ids().agent_registry, "get", [sc.sym("a1")]) is None

    debug = _rpc_lines(caplog, logging.DEBUG)
    assert len(debug) == 1, debug
    line = debug[0]
    assert "read agent_registry.get ok in" in line
    assert "load_ms=" in line and "sim_ms=" in line


def test_successful_read_never_logs_at_info(fake_rpc, caplog):
    """Volume guard: the metrics poll fans out twelve reads at a time, so a
    success must not reach INFO (the level production actually runs at)."""
    fake_rpc()
    with caplog.at_level(logging.DEBUG, logger=RPC_LOGGER):
        for _ in range(3):
            sc.simulate_read(sc.contract_ids().agent_registry, "get", [sc.sym("a1")])

    assert not [rec for rec in caplog.records if rec.name == RPC_LOGGER and rec.levelno >= logging.INFO]


def test_failed_read_logs_error_naming_the_stage(fake_rpc, caplog):
    fake_rpc(fail_on="load_account")
    with caplog.at_level(logging.DEBUG, logger=RPC_LOGGER), pytest.raises(RuntimeError):
        sc.simulate_read(sc.contract_ids().agent_registry, "get", [])

    errors = _rpc_lines(caplog, logging.ERROR)
    assert len(errors) == 1, errors
    # Which hop died, and why — the whole point of the instrumentation.
    assert "read agent_registry.get failed in" in errors[0]
    assert "stage=load_account" in errors[0]
    assert "429 too many requests" in errors[0]


def test_read_over_slow_threshold_logs_warning(caplog):
    """A throttled RPC is the precursor to executor saturation, so it is a
    WARNING even though the call ultimately succeeded."""
    with caplog.at_level(logging.DEBUG, logger=RPC_LOGGER):
        sc._log_rpc("read", "agent_registry.get", sc.SLOW_READ_MS + 1, {}, slow_ms=sc.SLOW_READ_MS)
        sc._log_rpc("read", "agent_registry.get", sc.SLOW_READ_MS - 1, {}, slow_ms=sc.SLOW_READ_MS)

    assert len(_rpc_lines(caplog, logging.WARNING)) == 1
    assert len(_rpc_lines(caplog, logging.DEBUG)) == 1


def test_submit_success_logs_at_info(caplog):
    """Money-path submits are rare and each moves value — worth an INFO line."""
    with caplog.at_level(logging.DEBUG, logger=RPC_LOGGER):
        sc._log_rpc("submit", "payment_escrow.charge", 12.0, {"tx": "abc"}, slow_ms=sc.SLOW_SUBMIT_MS, notable=True)

    info = _rpc_lines(caplog, logging.INFO)
    assert len(info) == 1
    assert "submit payment_escrow.charge ok in" in info[0]
    assert "tx=abc" in info[0]


def test_envelope_identity_never_leaks_the_xdr():
    """The log label for a signed envelope is derived, public data only."""
    from stellar_sdk import Account, Keypair, Network, TransactionBuilder

    kp = Keypair.random()
    tx = (
        TransactionBuilder(
            source_account=Account(kp.public_key, 1),
            network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
            base_fee=100,
        )
        .append_bump_sequence_op(2)
        .set_timeout(30)
        .build()
    )
    tx.sign(kp)
    xdr = tx.to_xdr()

    tx_hash, source = sc.envelope_identity(xdr)
    assert source == kp.public_key
    assert len(tx_hash) == 64
    assert tx_hash not in xdr and source not in xdr
    # Undecodable input must degrade to placeholders, never raise.
    assert sc.envelope_identity("not-xdr") == ("unparseable", "unknown")


# ── money-path failure context ─────────────────────────────────────────
# A stack trace from /server/charge, /server/seal or /submit has to name the
# transaction it belongs to, and must never carry key material.
ROUTER_LOGGER = "app.routers.stellar"

CHARGE_BODY = {"auth_id_hex": "ab" * 16, "amount_usdc": 1.0, "job_id_hex": "cd" * 16}
SEAL_BODY = {
    "job_id_hex": "cd" * 16,
    "orchestrator": "G" + "A" * 55,
    "intent_hash_hex": "ef" * 32,
    "agents": ["agt_01h8", "agt_02k2"],
    "receipts_hex": ["11" * 16],
    "total_spent_usdc": 2.5,
}


@pytest.fixture()
def server_signing_key(hermetic_settings):
    """Give the backend a throwaway signing key for one test, clearing the
    lru_cached keypair on both sides so it never leaks into another test."""
    from stellar_sdk import Keypair

    kp = Keypair.random()
    sc._signer_keypair.cache_clear()
    hermetic_settings.stellar_signing_key = kp.secret
    hermetic_settings.max_charge_usdc = 100.0
    yield kp
    sc._signer_keypair.cache_clear()


@pytest.fixture()
def failing_invoke(monkeypatch):
    """Make the backend-signed invoke blow up without touching the network."""

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("escrow: authorization expired")

    monkeypatch.setattr(sc, "invoke_with_server_key_async", _boom)


def _router_errors(caplog) -> str:
    return "\n".join(rec.getMessage() for rec in caplog.records if rec.name == ROUTER_LOGGER)


def test_charge_failure_log_carries_transaction_ids(client, server_signing_key, failing_invoke, caplog):
    with caplog.at_level(logging.ERROR, logger=ROUTER_LOGGER):
        r = client.post("/api/stellar/server/charge", json=CHARGE_BODY)
    assert r.status_code == 400

    line = _router_errors(caplog)
    assert "server charge failed" in line
    assert f"auth_id={CHARGE_BODY['auth_id_hex']}" in line
    assert f"job_id={CHARGE_BODY['job_id_hex']}" in line
    assert "amount_usdc=1.0" in line
    # caplog.text includes the formatted traceback, so this covers the whole
    # record, not just the message.
    assert server_signing_key.secret not in caplog.text


def test_seal_failure_log_carries_transaction_ids(client, server_signing_key, failing_invoke, caplog, monkeypatch):
    # The seal fails closed without an operator key; this is the opted-out
    # local testnet shape, so the signing path itself is what is exercised.
    monkeypatch.setattr(settings, "allow_keyless_server_seal", True)
    with caplog.at_level(logging.ERROR, logger=ROUTER_LOGGER):
        r = client.post("/api/stellar/server/seal", json=SEAL_BODY)
    assert r.status_code == 400

    line = _router_errors(caplog)
    assert "server seal failed" in line
    assert f"job_id={SEAL_BODY['job_id_hex']}" in line
    assert f"orchestrator={SEAL_BODY['orchestrator']}" in line
    assert "total_spent_usdc=2.5" in line
    assert server_signing_key.secret not in caplog.text


def test_submit_failure_log_identifies_tx_without_leaking_the_xdr(client, monkeypatch, caplog):
    from stellar_sdk import Account, Keypair, Network, TransactionBuilder

    kp = Keypair.random()
    tx = (
        TransactionBuilder(
            source_account=Account(kp.public_key, 1),
            network_passphrase=Network.TESTNET_NETWORK_PASSPHRASE,
            base_fee=100,
        )
        .append_bump_sequence_op(2)
        .set_timeout(30)
        .build()
    )
    tx.sign(kp)
    signed_xdr = tx.to_xdr()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("rpc unreachable")

    monkeypatch.setattr(sc, "submit_signed_xdr_async", _boom)

    with caplog.at_level(logging.ERROR, logger=ROUTER_LOGGER):
        r = client.post("/api/stellar/submit", json={"signed_xdr": signed_xdr})
    assert r.status_code == 400

    line = _router_errors(caplog)
    assert "signed xdr submit failed" in line
    assert f"source={kp.public_key}" in line
    tx_hash = line.split("tx_hash=")[1].split()[0]
    assert len(tx_hash) == 64 and tx_hash != "unparseable"
    # The envelope carries the user's signature — it must stay out of the log.
    assert signed_xdr not in caplog.text
    assert kp.secret not in caplog.text


def test_diagnostic_decode_failures_are_reported_not_swallowed():
    """`except Exception: continue` used to drop these silently; the count is
    now folded into the summary the caller logs and returns."""

    class _Status:
        diagnostic_events_xdr = ["not-a-diagnostic-event", "also-garbage"]
        result_xdr = None

    summary = sc._extract_diagnostics(_Status())
    assert "2 undecodable diagnostic event(s)" in summary
