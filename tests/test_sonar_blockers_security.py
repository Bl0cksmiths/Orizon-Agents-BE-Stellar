"""Behaviour pinned while clearing SonarQube blockers in `app/security.py`.

`BodyLimitMiddleware` carries a class-level table of per-path defaults and an
instance-level `path_limits` (that default, or a caller's override); the two
must stay distinct in both name and effect.

`SecretRedactionLogFilter.filter` returns the record to log (Python 3.12+)
rather than a constant `True`; it must still never drop a record.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.security import REDACTED, BodyLimitMiddleware, SecretRedactionLogFilter

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


def test_each_middleware_gets_its_own_copy_of_the_default_path_limits() -> None:
    # The class table is shared by every instance; the instance's table must
    # not be, or tuning one middleware would retune all of them.
    middleware = BodyLimitMiddleware(_byte_counting_app())

    assert middleware.path_limits == BodyLimitMiddleware.DEFAULT_PATH_LIMITS
    assert middleware.path_limits is not BodyLimitMiddleware.DEFAULT_PATH_LIMITS
    middleware.path_limits[PDAX_WEBHOOK] = 1

    assert BodyLimitMiddleware.DEFAULT_PATH_LIMITS[PDAX_WEBHOOK] == PDAX_WEBHOOK_CAP


SECRET = "correct-horse-battery-staple"


class _CollectingHandler(logging.Handler):
    """Keeps every record it is asked to emit, unformatted, so a test can see
    what got past the filters even when the record cannot be formatted."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def redacting_handler(monkeypatch: pytest.MonkeyPatch) -> _CollectingHandler:
    """A handler wired the way `app/main.py` wires the root handler. Tests
    drive it with `Handler.handle` — filter, then emit — rather than through
    a logger, so no other handler (pytest's capture among them) sees the
    record and nothing global is left changed."""
    monkeypatch.setattr(settings, "pdax_password", SECRET)
    handler = _CollectingHandler()
    handler.addFilter(SecretRedactionLogFilter())
    return handler


def _record(msg: str, *args: object, exc_info: Any = None) -> logging.LogRecord:
    return logging.LogRecord("pdax", logging.ERROR, __file__, 1, msg, args, exc_info)


def test_the_filter_returns_the_record_it_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "pdax_password", SECRET)
    record = _record("login failed with %s", SECRET)

    returned = SecretRedactionLogFilter().filter(record)

    assert isinstance(returned, logging.LogRecord)
    assert returned is record
    assert returned.getMessage() == f"login failed with {REDACTED}"


def test_a_secret_in_the_message_is_masked_and_the_record_still_emitted(
    redacting_handler: _CollectingHandler,
) -> None:
    redacting_handler.handle(_record("login failed with %s", SECRET))

    [emitted] = redacting_handler.records
    assert emitted.getMessage() == f"login failed with {REDACTED}"
    assert SECRET not in redacting_handler.format(emitted)


def test_a_secret_in_a_traceback_is_masked_and_the_record_still_emitted(
    redacting_handler: _CollectingHandler,
) -> None:
    try:
        raise ConnectionError(f"pdax rejected {SECRET}")
    except ConnectionError:
        record = _record("pdax login failed", exc_info=sys.exc_info())

    redacting_handler.handle(record)

    [emitted] = redacting_handler.records
    rendered = redacting_handler.format(emitted)
    assert SECRET not in rendered
    assert f"pdax rejected {REDACTED}" in rendered
    assert rendered.startswith("pdax login failed\nTraceback")
    # Folded into the message, so no formatter can render the raw traceback.
    assert emitted.exc_info is None


def test_a_malformed_record_is_still_emitted_unchanged(redacting_handler: _CollectingHandler) -> None:
    # `getMessage()` raises on these args. Reporting that is logging's job
    # (Handler.handleError, at format time); the filter must not drop it.
    msg = "plan has %d steps"
    record = _record(msg, "many")

    redacting_handler.handle(record)

    [emitted] = redacting_handler.records
    assert emitted is record
    assert emitted.msg is msg
    assert emitted.args == ("many",)
    with pytest.raises(TypeError):
        emitted.getMessage()


def test_the_filter_returns_a_malformed_record_as_given() -> None:
    record = _record("plan has %d steps", "many")

    assert SecretRedactionLogFilter().filter(record) is record
    assert record.args == ("many",)
