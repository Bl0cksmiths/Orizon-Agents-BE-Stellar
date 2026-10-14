"""Behaviour pinned while clearing SonarQube blockers in `app/security.py`.

`BodyLimitMiddleware` carries a class-level table of per-path defaults and an
instance-level `path_limits` (that default, or a caller's override); the two
must stay distinct in both name and effect.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.security import BodyLimitMiddleware

PDAX_WEBHOOK = "/api/pdax/webhooks/receive"
PDAX_WEBHOOK_CAP = 65_536


def _byte_counting_app() -> Any:
    """Raw ASGI app that reads the whole body and answers with its length."""

    async def app(scope: dict, receive: Any, send: Any) -> None:
        total, more = 0, True
        while more:
            message = await receive()
            total += len(message.get("body", b""))
            more = message.get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(total).encode()})

    return app


def test_the_pdax_webhook_gets_its_64_kib_cap_by_default() -> None:
    c = TestClient(BodyLimitMiddleware(_byte_counting_app()))

    assert c.post(PDAX_WEBHOOK, content=b"x" * PDAX_WEBHOOK_CAP).status_code == 200
    assert c.post(PDAX_WEBHOOK, content=b"x" * (PDAX_WEBHOOK_CAP + 1)).status_code == 413
    # Every other path keeps the global 1 MiB budget.
    assert c.post("/echo", content=b"x" * (PDAX_WEBHOOK_CAP + 1)).status_code == 200


def test_explicit_path_limits_replace_the_defaults_rather_than_merging() -> None:
    c = TestClient(BodyLimitMiddleware(_byte_counting_app(), path_limits={"/echo": 10}))

    assert c.post("/echo", content=b"x" * 11).status_code == 413
    # The PDAX default is gone, not merged in: that path falls back to the
    # global limit.
    assert c.post(PDAX_WEBHOOK, content=b"x" * (PDAX_WEBHOOK_CAP + 1)).status_code == 200


def test_an_empty_override_lifts_every_per_path_cap() -> None:
    c = TestClient(BodyLimitMiddleware(_byte_counting_app(), path_limits={}))

    assert c.post(PDAX_WEBHOOK, content=b"x" * (PDAX_WEBHOOK_CAP + 1)).status_code == 200
