"""Story 1.06 spike — prove Candidate A actually dispatches a step.

These tests stand up an in-process operator endpoint (httpx.ASGITransport over
a tiny FastAPI app — a real request/response cycle, no sockets) and drive the
ExternalHttpWorker against it, including THROUGH the real orchestrator loop
(`execution_svc._run`) so the acceptance criterion "its response should be
accepted by the orchestrator" is met by running code, not prose.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.agents.workers.external_http import MAX_RESPONSE_BYTES, ExternalDispatchError, ExternalHttpWorker
from app.schemas import Plan, PlanStep, StoredPlan, Task
from app.services import execution_svc
from app.state import state

# A public https URL: the worker validates its endpoint before every dispatch
# (SSRF guard), and the injected ASGI/Mock transports below never open a
# socket, so the URL is only a label for the in-process endpoint.
ENDPOINT = "https://operator.example/run"


def _client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://operator.example")


def _resolves_to(monkeypatch: pytest.MonkeyPatch, lookup) -> None:
    """Pin the dispatch seam to `lookup`.

    execution_svc resolves a step's worker through the async `resolve_worker`
    (local registry first, then a stored binding). The spike predates that
    seam and only needs one worker handed back for one agent id, so the sync
    lookup is adapted here; the binding path itself is covered by
    tests/test_external_agent_dispatch.py.
    """

    async def _resolve(agent_id: str):
        return lookup(agent_id)

    monkeypatch.setattr(execution_svc, "resolve_worker", _resolve)


def _worker(client: httpx.AsyncClient) -> ExternalHttpWorker:
    return ExternalHttpWorker("ext_demo1", "external.demo", ENDPOINT, client=client)


def test_dispatch_carries_the_envelope_and_returns_the_output() -> None:
    seen: dict[str, Any] = {}
    app = FastAPI()

    @app.post("/run")
    async def run(req: Request) -> JSONResponse:
        seen["body"] = await req.json()
        seen["idempotency_key"] = req.headers.get("Idempotency-Key")
        seen["content_type"] = req.headers.get("Content-Type")
        return JSONResponse(
            {"summary": "built the landing page", "artifact": {"title": "x", "files": []}, "source": "external"}
        )

    async def go() -> dict[str, Any]:
        async with _client_for(app) as client:
            return await _worker(client).run("make a bakery landing page", "draft it", context={"kit": None})

    out = asyncio.run(go())

    assert out["summary"] == "built the landing page"
    # Stamped by us, not carried from the operator's response — see _parse.
    assert out["source"] == "external"

    body = seen["body"]
    # v2 since story 2.02 (ADR 0004): the envelope gained `ts` and `network`,
    # both inside the signed bytes.
    assert body["v"] == 2
    assert isinstance(body["ts"], int) and body["ts"] > 0
    assert body["network"]
    assert body["agent_id"] == "ext_demo1"
    assert body["intent"] == "make a bakery landing page"
    assert body["rationale"] == "draft it"
    assert body["context"] == {"kit": None}
    assert body["dispatch_id"]
    # the idempotency key the operator can dedupe on IS the dispatch id
    assert seen["idempotency_key"] == body["dispatch_id"]
    assert seen["content_type"] == "application/json"


def test_orchestrator_accepts_the_external_dispatch_response(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole point of the spike: a step routed to an EXTERNAL agent id — one
    # with no local worker — is dispatched over HTTP and its response flows back
    # through execution_svc._run as a completed, billed step. This test proves
    # the worker end by injecting it directly; story 2.01 since closed the other
    # end, so a BOUND id now resolves through binding_registry.resolve_worker
    # instead of hitting the `unknown agent` skip. An unbound id still skips —
    # see tests/test_external_agent_dispatch.py for both paths end to end.
    app = FastAPI()

    @app.post("/run")
    async def run(req: Request) -> JSONResponse:
        body = await req.json()
        return JSONResponse(
            {
                "summary": f"external agent handled: {body['intent']}",
                "artifact": {"title": "landing.html", "files": [{"content": "<!doctype html>\n<html></html>\n"}]},
                "source": "external",
            }
        )

    async def go() -> None:
        async with _client_for(app) as client:
            worker = _worker(client)
            _resolves_to(monkeypatch, lambda aid: worker if aid == "ext_demo1" else None)

            plan = StoredPlan(
                id="pln_ext_spike",
                intent="build a bakery landing page",
                plan=Plan(
                    steps=[
                        PlanStep(
                            agent_id="ext_demo1",
                            agent_name="external.demo",
                            rationale="build the page",
                            est_price_usdc=0.02,
                            est_eta_seconds=1.0,
                        )
                    ]
                ),
                total_usdc=0.02,
                total_eta=1.0,
            )
            state.add_plan(plan)
            state.add_task(Task(id="tsk_ext_spike", intent=plan.intent, agents=1, spent=0.0, status="running"))
            await execution_svc._run(plan, "tsk_ext_spike")

    asyncio.run(go())

    final = state.tasks["tsk_ext_spike"]
    assert final.status == "complete"
    assert final.spent == 0.02  # the external step was billed at its registered price
    assert final.artifact is not None
    assert final.artifact["title"] == "landing.html"


def test_operator_error_fails_the_step_unbilled(monkeypatch: pytest.MonkeyPatch) -> None:
    # A failing external endpoint must degrade exactly like a failing local
    # worker: step skipped, nothing billed, the workflow does not crash.
    app = FastAPI()

    @app.post("/run")
    async def run(_req: Request) -> JSONResponse:
        return JSONResponse({"error": {"code": "boom", "message": "operator is down"}}, status_code=500)

    async def go() -> None:
        async with _client_for(app) as client:
            worker = _worker(client)
            _resolves_to(monkeypatch, lambda aid: worker if aid == "ext_demo1" else None)
            plan = StoredPlan(
                id="pln_ext_fail",
                intent="build a bakery landing page",
                plan=Plan(
                    steps=[
                        PlanStep(
                            agent_id="ext_demo1",
                            agent_name="external.demo",
                            rationale="build the page",
                            est_price_usdc=0.02,
                            est_eta_seconds=1.0,
                        )
                    ]
                ),
                total_usdc=0.02,
                total_eta=1.0,
            )
            state.add_plan(plan)
            state.add_task(Task(id="tsk_ext_fail", intent=plan.intent, agents=1, spent=0.0, status="running"))
            await execution_svc._run(plan, "tsk_ext_fail")

    asyncio.run(go())

    final = state.tasks["tsk_ext_fail"]
    assert final.status == "failed"
    assert final.spent == 0.0
    assert final.artifact is None


def test_response_missing_summary_is_rejected() -> None:
    app = FastAPI()

    @app.post("/run")
    async def run(_req: Request) -> JSONResponse:
        return JSONResponse({"artifact": {"title": "x", "files": []}})  # no non-empty summary

    async def go() -> None:
        async with _client_for(app) as client:
            with pytest.raises(ExternalDispatchError):
                await _worker(client).run("x", "y")

    asyncio.run(go())


def test_connect_failure_retries_once_then_raises() -> None:
    # No response was ever received, so the worker retries exactly once (safe:
    # the operator never got the step) and then fails the dispatch.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("no route to host", request=request)

    async def go() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://operator.example") as client:
            with pytest.raises(ExternalDispatchError):
                await _worker(client).run("x", "y")

    asyncio.run(go())
    assert calls["n"] == 2  # original attempt + exactly one retry


def test_oversize_response_is_rejected() -> None:
    app = FastAPI()

    @app.post("/run")
    async def run(_req: Request) -> JSONResponse:
        return JSONResponse({"summary": "x" * (MAX_RESPONSE_BYTES + 1024)})  # over the size cap

    async def go() -> None:
        async with _client_for(app) as client:
            with pytest.raises(ExternalDispatchError):
                await _worker(client).run("x", "y")

    asyncio.run(go())


def test_non_json_response_is_rejected() -> None:
    app = FastAPI()

    @app.post("/run")
    async def run(_req: Request) -> PlainTextResponse:
        return PlainTextResponse("this is not json")

    async def go() -> None:
        async with _client_for(app) as client:
            with pytest.raises(ExternalDispatchError):
                await _worker(client).run("x", "y")

    asyncio.run(go())


def test_non_object_json_response_is_rejected() -> None:
    app = FastAPI()

    @app.post("/run")
    async def run(_req: Request) -> JSONResponse:
        return JSONResponse([1, 2, 3])  # valid JSON, but an array not an object

    async def go() -> None:
        async with _client_for(app) as client:
            with pytest.raises(ExternalDispatchError):
                await _worker(client).run("x", "y")

    asyncio.run(go())
