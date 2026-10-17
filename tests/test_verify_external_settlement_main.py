"""Characterization tests for `scripts/verify_external_settlement.py`'s `main`.

The script reads the marketplace API and Horizon. Here every request goes to
an `httpx.MockTransport` swapped in for the module's client, so nothing leaves
the process. Each case pins the requests made, in order, the whole report and
the exit code.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scripts import verify_external_settlement as ves

API = "https://api.test"
HORIZON = "https://horizon-testnet.stellar.org"
AGENT = "ext_alpha"
OWNER = "GOWNER"
ADMIN = "GADMIN"
TX = "ab" * 32
CONFIG = {"network": "testnet", "asset_sac": "CSAC", "asset": "XLM", "admin": ADMIN, "dispatch_signer": "GDISPATCH"}


def _ok(body: Any) -> tuple[int, Any]:
    return 200, body


def _records(*records: dict[str, Any]) -> dict[str, Any]:
    return {"_embedded": {"records": list(records)}}


HEALTHY: dict[str, tuple[int, Any]] = {
    f"{API}/api/stellar/network": _ok(CONFIG),
    f"{API}/api/stellar/agent/{AGENT}": _ok({"agent": {"owner": OWNER}}),
    f"{API}/api/agents/{AGENT}/binding": _ok({"agent_id": AGENT, "owner": OWNER, "endpoint_url": "https://op.test"}),
    f"{HORIZON}/transactions/{TX}": _ok({"successful": True}),
    f"{HORIZON}/transactions/{TX}/operations": _ok(
        _records(
            {
                "id": "op1",
                "type": "invoke_host_function",
                "asset_balance_changes": [
                    {"type": "transfer", "from": ADMIN, "to": OWNER, "amount": "0.0540000", "asset_type": "native"}
                ],
            }
        )
    ),
    f"{HORIZON}/transactions/{TX}/effects": _ok(_records()),
}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: dict[str, tuple[int, Any]],
    *extra: str,
) -> tuple[int, list[str], list[str]]:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url.copy_with(query=None))
        requests.append(str(request.url))
        status, body = answers.get(url, (404, {"status": 404}))
        return httpx.Response(status, json=body)

    def client(**kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(ves, "httpx", SimpleNamespace(Client=client))
    argv = ["verify_external_settlement.py", "--agent", AGENT, "--owner", OWNER, "--tx", TX, "--api-base", API + "/"]
    monkeypatch.setattr(sys, "argv", [*argv, *extra])
    code = ves.main()
    return code, capsys.readouterr().out.splitlines(), requests


TAIL = [
    "",
    f"  stellar.expert (tx):      https://stellar.expert/explorer/testnet/tx/{TX}",
    f"  stellar.expert (account): https://stellar.expert/explorer/testnet/account/{OWNER}",
    "",
]


def test_a_settlement_that_holds_passes(monkeypatch, capsys):
    code, out, requests = _run(monkeypatch, capsys, HEALTHY, "--amount", "0.054")
    assert code == 0
    assert requests == [
        f"{API}/api/stellar/network",
        f"{API}/api/stellar/agent/{AGENT}",
        f"{API}/api/agents/{AGENT}/binding",
        f"{HORIZON}/transactions/{TX}",
        f"{HORIZON}/transactions/{TX}/operations?limit=200",
    ]
    assert out == [
        "",
        f"External settlement evidence — agent {AGENT} - tx {TX[:12]}...",
        "",
        "  [PASS] network_config_read: asset SAC CSAC",
        "  [PASS] network_matches: API network 'testnet' matches --network 'testnet'",
        f"  [PASS] agent_onchain: {AGENT} is registered on AgentRegistry",
        "  [PASS] owner_matches: owner_of matches the credited account",
        "  [PASS] binding_present: bound to https://op.test",
        "  [PASS] binding_owner_matches: binding owner matches the credited account",
        "  [PASS] tx_on_horizon: transaction found on Horizon",
        "  [PASS] tx_succeeded: transaction successful on-chain",
        f"  [PASS] owner_credited: 0.0540000 XLM (native) credited to {OWNER} — "
        "asset_balance_changes on operation op1 (invoke_host_function)",
        "  [PASS] credited_amount: credited 0.0540000 as expected",
        "",
        "  asset SAC (configured):   CSAC",
        "  asset (API reports):      XLM",
        "  asset (this credit):      XLM (native)",
        "  ^ NOT USDC. Label this payout with the asset above — never as USDC.",
        f"  funded by:                {ADMIN}",
        "  ^ the deployment's admin key: a PLATFORM-FUNDED payout, not an escrow settlement.",
        "  note: PaymentEscrow.charge cannot settle an external payer's funds on this deployment -",
        "        see docs/evidence/2.04-reference-agent-runbook.md, 'Settlement position'.",
        *TAIL,
        "  VERDICT: PASS - settlement verified",
        "",
    ]


def test_every_read_failing_is_a_failed_check_each(monkeypatch, capsys):
    answers = {url: (503, {}) for url in HEALTHY}
    code, out, requests = _run(monkeypatch, capsys, answers)
    assert code == 1
    assert requests == [
        f"{API}/api/stellar/network",
        f"{API}/api/stellar/agent/{AGENT}",
        f"{API}/api/agents/{AGENT}/binding",
        f"{HORIZON}/transactions/{TX}",
    ]
    assert out == [
        "",
        f"External settlement evidence — agent {AGENT} - tx {TX[:12]}...",
        "",
        "  [FAIL] network_config_read: /api/stellar/network returned 503",
        f"  [FAIL] agent_onchain: /api/stellar/agent/{AGENT} returned 503",
        f"  [FAIL] binding_present: /api/agents/{AGENT}/binding returned 503",
        "  [FAIL] tx_on_horizon: Horizon returned 503 for the tx hash",
        "",
        "  asset SAC (configured):   -",
        "  asset (API reports):      -",
        *TAIL,
        "  VERDICT: FAIL - see checks above",
        "",
    ]


def test_a_classic_payment_is_found_in_the_effects(monkeypatch, capsys):
    answers = dict(HEALTHY)
    answers[f"{HORIZON}/transactions/{TX}/operations"] = (500, {})
    answers[f"{HORIZON}/transactions/{TX}/effects"] = _ok(
        _records(
            {"id": "e2", "type": "account_debited", "account": "GBUYER", "amount": "1.0", "asset_type": "native"},
            {
                "id": "e1",
                "type": "account_credited",
                "account": OWNER,
                "amount": "1.0",
                "asset_code": "USDC",
                "asset_issuer": "GISSUER",
            },
        )
    )
    code, out, requests = _run(monkeypatch, capsys, answers, "--amount", "2")
    assert code == 1
    assert requests[-2:] == [
        f"{HORIZON}/transactions/{TX}/operations?limit=200",
        f"{HORIZON}/transactions/{TX}/effects?limit=200",
    ]
    assert f"  [PASS] owner_credited: 1.0 USDC (issuer GISSUER) credited to {OWNER} — account_credited effect e1" in out
    assert "  [FAIL] credited_amount: credited 1.0, expected 2.0" in out
    assert "  asset (this credit):      USDC (issuer GISSUER)" in out
    assert "  funded by:                GBUYER" in out
    assert "  ^ not a key named by /api/stellar/network; confirm whose wallet this is before labelling it." in out


def test_no_credit_anywhere_fails_the_owner_check(monkeypatch, capsys):
    answers = dict(HEALTHY)
    answers[f"{HORIZON}/transactions/{TX}/operations"] = _ok(_records({"id": "op1", "type": "payment"}))
    answers[f"{HORIZON}/transactions/{TX}/effects"] = (404, {})
    answers[f"{API}/api/stellar/network"] = _ok({**CONFIG, "network": "mainnet"})
    answers[f"{HORIZON}/transactions/{TX}"] = _ok({"successful": False})
    code, out, _ = _run(monkeypatch, capsys, answers)
    assert code == 1
    assert "  [FAIL] network_matches: API is on 'mainnet' but the tx was read from 'testnet' Horizon" in out
    assert "  [FAIL] tx_succeeded: tx not successful (successful=False)" in out
    assert f"  [FAIL] owner_credited: no credit to {OWNER} in this transaction's operations or effects" in out
    assert not any(line.startswith("  funded by:") for line in out)
