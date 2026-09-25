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

`TaskReadProof` is the same question asked WITHOUT the switch: "has this
caller proved they may read this task's private material?" `require_task_read`
turns a no into a 404 and only when enforcement is on; a proof turns a no into
LESS in the response, always. That is what `routers/disputes.py` withholds a
buyer's free text on — a route that must stay readable to a shared trace link,
carrying two fields that must not be.

Lives outside app/security.py so the task-token concern stays separable
from the global API-key/rate-limit hardening primitives.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException, Query

from .config import settings
from .security import header_secret_matches
from .state import state


def _matches(candidate: str, expected: str) -> bool:
    # For the TASK TOKEN only, which this module mints itself and which is
    # hex — so utf-8 and latin-1 agree on every byte of it, and a candidate
    # that is not ASCII is simply not the token. `security.header_secret_matches`
    # is the one to use for anything whose bytes came off a header and may not
    # be ASCII; it cannot be used here because `token` may arrive as a QUERY
    # parameter instead, which Starlette decodes as utf-8, not latin-1.
    return secrets.compare_digest(candidate.encode("utf-8", "ignore"), expected.encode("utf-8"))


@dataclass(frozen=True)
class TaskReadProof:
    """What one request offered as proof, resolved once and asked per task.

    Two credentials, exactly the two `require_task_read` accepts, so "may this
    caller read this task?" has ONE answer in this service however it is asked.
    Frozen and inert: it holds what arrived, decides nothing at construction,
    and every decision it makes is `proves(task_id)`.

    Kept apart from `require_task_read` because the task id is not always known
    when the request is dispatched. `GET /api/disputes/{id}` is scoped to a
    task the caller never names — the dispute record does — so the credentials
    have to be carried into the handler and asked there, once the record is
    loaded.
    """

    task_token: str | None
    api_key: str | None

    def proves(self, task_id: str) -> bool:
        """True when this caller may read `task_id`'s private material.

        Independent of TASK_AUTH_REQUIRED, and that is the point: the switch
        decides whether a task's reads are gated at all, while this decides
        what goes into a response that is being served either way. Tying the
        two would mean the shipped default — enforcement off — published
        every buyer's words to anybody who asked.

        The operator key first, and it is checked against `settings.api_key`
        directly: an unset key proves nothing, which `header_secret_matches`
        answers for us rather than admitting everyone on a demo deployment.
        """
        if header_secret_matches(self.api_key, settings.api_key):
            return True
        expected = state.task_tokens.get(task_id)
        return expected is not None and self.task_token is not None and _matches(self.task_token, expected)


async def task_read_proof(
    x_task_token: str | None = Header(default=None, alias="X-Task-Token"),
    token: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> TaskReadProof:
    """FastAPI dependency resolving one request's task-read credentials.

    Accepts the token from the query as well as the header, for
    `require_task_read`'s reason — EventSource cannot set headers — so a route
    reachable from a stream is not a route with a second, weaker answer.
    """
    return TaskReadProof(task_token=x_task_token if x_task_token is not None else token, api_key=x_api_key)


async def require_task_read(
    task_id: str,
    x_task_token: str | None = Header(default=None, alias="X-Task-Token"),
    token: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency guarding a `{task_id}`-scoped read route."""
    if not settings.task_auth_required:
        return
    # A valid operator API key sees everything (ops visibility). Through
    # `header_secret_matches`, so an operator whose key holds a non-ASCII
    # character is admitted here on exactly the terms `require_api_key` admits
    # them — two answers to "is this the operator?" would be one too many.
    if header_secret_matches(x_api_key, settings.api_key):
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
