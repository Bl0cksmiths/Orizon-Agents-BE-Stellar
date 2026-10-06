"""The lifecycle harness's API client and chain reader (story 5.01): retry reads, never writes."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from stellar_sdk import scval

from scripts.lifecycle.api import ApiError, OrizonApi, UnknownOutcome
from scripts.lifecycle.chain import ChainReader, SimulationError
from scripts.lifecycle.config import TESTNET_PASSPHRASE, normalize_api_base
from scripts.lifecycle.retry import RetryPolicy

BASE = "https://api.fake"
RPC = "https://rpc.fake"
HORIZON = "https://horizon.fake"


def _api(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[OrizonApi, list[float]]:
    slept: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OrizonApi(client=client, base=BASE, retry=RetryPolicy(sleep=slept.append)), slept


def _envelope(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": code, "error": {"code": code, "message": code, "request_id": "r"}})


def _sequence(*responses: httpx.Response | Exception) -> tuple[Callable[[httpx.Request], httpx.Response], list[str]]:
    seen: list[str] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return handler, seen


# ── reads retry ─────────────────────────────────────────────────
def test_a_read_retries_a_sleeping_backend_with_backoff() -> None:
    handler, seen = _sequence(
        httpx.Response(503, text="<html>"),
        httpx.ConnectError("refused"),
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(200, json={"network_passphrase": TESTNET_PASSPHRASE}),
    )
    api, slept = _api(handler)
    assert api.network()["network_passphrase"] == TESTNET_PASSPHRASE
    assert len(seen) == 4
    assert slept == [2.0, 4.0, 7.0]


def test_a_read_gives_up_after_its_budget() -> None:
    handler, seen = _sequence(httpx.Response(503, text="down"))
    api, _ = _api(handler)
    with pytest.raises(ApiError, match="still failing"):
        api.agents()
    assert len(seen) == 4


def test_a_read_does_not_retry_an_answer() -> None:
    handler, seen = _sequence(_envelope(404, "unknown_task"))
    api, _ = _api(handler)
    with pytest.raises(ApiError) as exc:
        api.task("tsk_1", "tok")
    assert exc.value.code == "unknown_task" and len(seen) == 1


# ── writes never retry ──────────────────────────────────────────
@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.submit("AAAA"),
        lambda api: api.execute("pln_1", "00" * 16, "GB"),
        lambda api: api.uphold("dsp_1", "key"),
        lambda api: api.open_dispute({}),
    ],
)
@pytest.mark.parametrize(
    "answer",
    [httpx.ReadTimeout("lost"), httpx.Response(502, text="<html>bad gateway</html>"), _envelope(500, "internal_error")],
)
def test_a_write_with_a_lost_answer_is_unknown_and_sent_once(call: Any, answer: Any) -> None:
    handler, seen = _sequence(answer, httpx.Response(200, json={}))
    api, slept = _api(handler)
    with pytest.raises(UnknownOutcome):
        call(api)
    assert len(seen) == 1 and slept == []


def test_submit_failed_is_unknown_because_it_may_have_been_sent() -> None:
    handler, seen = _sequence(_envelope(400, "submit_failed"))
    api, _ = _api(handler)
    with pytest.raises(UnknownOutcome):
        api.submit("AAAA")
    assert len(seen) == 1


def test_refund_unconfirmed_is_unknown() -> None:
    handler, _ = _sequence(_envelope(504, "refund_unconfirmed"))
    api, _ = _api(handler)
    with pytest.raises(UnknownOutcome):
        api.uphold("dsp_1", "key")


def test_an_app_refusal_on_a_write_is_definite() -> None:
    handler, seen = _sequence(_envelope(503, "capacity_exhausted"))
    api, _ = _api(handler)
    with pytest.raises(ApiError) as exc:
        api.execute("pln_1", "00" * 16, "GB")
    assert exc.value.code == "capacity_exhausted" and len(seen) == 1
    handler, _ = _sequence(_envelope(502, "refund_failed"))
    api, _ = _api(handler)
    with pytest.raises(ApiError):
        api.uphold("dsp_1", "key")


def test_a_duplicate_dispute_answers_with_the_original() -> None:
    body = {"detail": "duplicate_dispute", "error": {"code": "duplicate_dispute"}, "dispute": {"id": "dsp_1"}}
    handler, _ = _sequence(httpx.Response(409, json=body))
    api, _ = _api(handler)
    assert api.open_dispute({}) == {"id": "dsp_1"}


def test_headers_and_bodies_are_the_dapps() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={})

    api, _ = _api(handler)
    api.task("tsk_1", "tok")
    api.task_disputes("tsk_1", None, "grant")
    api.dispute("dsp_1", grant="grant")
    api.uphold("dsp_1", "opkey")
    api.build_authorize("GB", 700_000, 600, "orizon_batch")
    assert captured[0].headers["x-task-token"] == "tok"
    assert captured[1].headers["x-dispute-read-grant"] == "grant" and "x-task-token" not in captured[1].headers
    assert captured[2].headers["x-dispute-read-grant"] == "grant"
    assert captured[3].headers["x-api-key"] == "opkey" and captured[3].url.path == "/api/disputes/dsp_1/uphold"
    assert captured[4].read() == (
        b'{"payer":"GB","agent_id":"orizon_batch","max_amount_stroops":700000,"ttl_seconds":600}'
    )


def test_readiness_is_best_effort() -> None:
    handler, _ = _sequence(httpx.ConnectError("no"))
    api, _ = _api(handler)
    assert api.readiness() is None


@pytest.mark.parametrize(
    ("raw", "base"),
    [
        ("https://orizons.xyz", "https://orizons.xyz"),
        ("https://orizons.xyz/", "https://orizons.xyz"),
        ("https://orizons.xyz/api/", "https://orizons.xyz"),
        (" https://x.test/gw/api ", "https://x.test/gw"),
    ],
)
def test_the_base_is_normalized_like_the_frontends(raw: str, base: str) -> None:
    assert normalize_api_base(raw) == base


def test_a_relative_base_is_refused() -> None:
    with pytest.raises(ValueError):
        normalize_api_base("orizons.xyz")


# ── chain reader ────────────────────────────────────────────────
def _chain(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[ChainReader, list[float]]:
    slept: list[float] = []
    clock = [0.0]

    def sleep(s: float) -> None:
        slept.append(s)
        clock[0] += s

    reader = ChainReader(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        rpc_url=RPC,
        horizon_url=HORIZON,
        passphrase=TESTNET_PASSPHRASE,
        retry=RetryPolicy(sleep=sleep),
        sleep=sleep,
        clock=lambda: clock[0],
    )
    return reader, slept


def _rpc(results: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "horizon.fake":
            record = results.get("horizon")
            return httpx.Response(404, json={}) if record is None else httpx.Response(200, json=record)
        payload = json.loads(request.content)
        answer = results[payload["method"]]
        answer = answer() if callable(answer) else answer
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], **answer})

    return handler


def test_observe_waits_for_a_pending_transaction() -> None:
    answers = iter([{"status": "NOT_FOUND"}, {"status": "NOT_FOUND"}, {"status": "SUCCESS", "ledger": 9}])
    reader, slept = _chain(_rpc({"getTransaction": lambda: {"result": next(answers)}}))
    seen = reader.observe("ab" * 32, budget=30)
    assert (seen.status, seen.ledger, seen.source) == ("SUCCESS", 9, "rpc")
    assert slept == [1.0, 2.0]


def test_observe_asks_horizon_once_the_rpc_has_forgotten() -> None:
    reader, _ = _chain(
        _rpc({"getTransaction": {"result": {"status": "NOT_FOUND"}}, "horizon": {"successful": False, "ledger": 4}})
    )
    seen = reader.observe("ab" * 32, budget=3)
    assert (seen.status, seen.source) == ("FAILED", "horizon")


def test_observe_reports_not_found_rather_than_guessing() -> None:
    reader, _ = _chain(_rpc({"getTransaction": {"result": {"status": "NOT_FOUND"}}}))
    assert reader.observe("ab" * 32, budget=3).status == "NOT_FOUND"


def test_escrow_version_reads_v2_and_falls_back_to_v1_only_on_a_simulation_error() -> None:
    v2 = {"results": [{"xdr": scval.to_uint32(2).to_xdr()}]}
    reader, _ = _chain(_rpc({"simulateTransaction": {"result": v2}}))
    assert (
        reader.escrow_version(
            "CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI",
            "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
        )[0]
        == 2
    )
    reader, _ = _chain(_rpc({"simulateTransaction": {"result": {"error": "HostError: MissingValue"}}}))
    assert (
        reader.escrow_version(
            "CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI",
            "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
        )[0]
        == 1
    )


def test_an_rpc_outage_is_never_read_as_v1() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    reader, _ = _chain(down)
    with pytest.raises(Exception) as exc:
        reader.escrow_version(
            "CBJPTMAPMGODGZCZ2IMEQSRUX3WGUXNMKDTNN2KMJ3NFGYZ5OJ5525PI",
            "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
        )
    assert not isinstance(exc.value, SimulationError)


def test_events_are_filtered_to_the_transaction_and_decoded() -> None:
    event = {
        "contractId": "C1",
        "txHash": "t1",
        "ledger": 5,
        "topic": [scval.to_symbol("charged").to_xdr(), scval.to_symbol("ext").to_xdr()],
        "value": scval.to_vec([scval.to_bytes(b"\x01" * 16), scval.to_int128(5)]).to_xdr(),
    }
    other = {**event, "txHash": "t2"}
    reader, _ = _chain(_rpc({"getEvents": {"result": {"events": [event, other]}}}))
    (found,) = reader.events("C1", 5, tx_hash="t1")
    assert found.topics == ["charged", "ext"] and found.value == ["01" * 16, 5]
