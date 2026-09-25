"""
Capability-token authorization for task-scoped reads.

Execute mints a per-task read token (services/execution_svc.execute_plan)
and returns it in ExecuteResponse. When TASK_AUTH_REQUIRED is on, the
task's read routes (status, artifact, trace, stream) demand that token —
via the `X-Task-Token` header or a `token` query parameter (EventSource
cannot set headers) — or a valid operator `X-API-Key`, which bypasses
per-task tokens for ops visibility. A missing or wrong token is a 404,
never a 403, so task ids stay unenumerable. Default OFF: the public demo
stays fully open.

Lives outside app/security.py so the task-token concern stays separable
from the global API-key/rate-limit hardening primitives.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Query

from .config import settings
from .state import state


def _matches(candidate: str, expected: str) -> bool:
    # Compare utf-8 bytes, not str — same rationale as security.require_api_key:
    # compare_digest raises TypeError on non-ASCII str input.
    return secrets.compare_digest(candidate.encode("utf-8", "ignore"), expected.encode("utf-8"))


async def require_task_read(
    task_id: str,
    x_task_token: str | None = Header(default=None, alias="X-Task-Token"),
    token: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency guarding a `{task_id}`-scoped read route."""
    if not settings.task_auth_required:
        return
    # A valid operator API key sees everything (ops visibility).
    if settings.api_key and x_api_key is not None and _matches(x_api_key, settings.api_key):
        return
    expected = state.task_tokens.get(task_id)
    supplied = x_task_token if x_task_token is not None else token
    if expected is None or supplied is None or not _matches(supplied, expected):
        # 404, not 403: a wrong token must be indistinguishable from a
        # nonexistent task, or ids become enumerable.
        #
        # A bare snake token, never the id. `main.http_exception_handler`
        # promotes a snake_case detail to `error.code` and derives the message
        # from it; an interpolated id is not a snake token, so it fell through
        # to the `not_found` fallback and the handler copied the CALLER'S OWN
        # TEXT into `error.message`. This dependency runs before the route's
        # own `max_length`, so that text was unbounded as well as reflected.
        # Nothing is lost: the id is in the path the caller sent, in the access
        # log line, and joinable to both through the request id.
        raise HTTPException(404, "unknown_task")
