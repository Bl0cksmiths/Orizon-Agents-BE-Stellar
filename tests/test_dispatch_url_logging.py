"""An operator's bound URL never reaches the logs through httpx.

External dispatch posts to the operator's bound endpoint with an
`httpx.AsyncClient`, and httpx logs "HTTP Request: POST <full URL>" at INFO for
every request a client sends. A bound URL can carry a query-string token, so at
the root's INFO level every dispatch wrote it into the logs — against ADR
0003's rule to log the host, never the URL. `app.main` holds httpx at WARNING;
this pins that it stays there, and that a real client request logs nothing.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

import app.main  # noqa: F401 — configures logging, as the service does at boot

BOUND = "https://operator.example/run?token=bound-url-secret-7f3a"


def test_httpx_is_held_at_warning_under_the_apps_logging() -> None:
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_a_dispatch_request_leaves_no_trace_of_its_url_in_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    async def send() -> int:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post(BOUND, json={"step": 0})
        return response.status_code

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(send()) == 200

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "bound-url-secret-7f3a" not in logged
    assert "operator.example" not in logged
