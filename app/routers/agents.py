"""Marketplace reads of the agent registry.

`bound` is stamped HERE, at read time, and never onto the `Agent` held in
`state.agents`. The stored copy is written by two things that run on their own
schedule — `seed` once at boot, and `registry_sync`'s poll of the chain — while
a binding changes whenever an operator binds, rebinds or repoints an endpoint.
Stamping at write time would therefore publish a `bound` that is correct only
until the next bind and not refreshed until the next sync pass, and a seeded
agent, which no pass ever revisits, would carry its boot-time answer forever.
Read time has no such window: the answer is computed for the request that asks.

It is also the cheap side. `binding_registry` keeps the bound ids in an
in-memory set, so this is one O(1) lookup per agent with no I/O, on a list the
page already fetches — which is the whole reason the field exists rather than
an endpoint the client would have to call once per agent.

Stamping is a COPY, never a mutation: `state.list_agents()` hands out the live
objects, so assigning to them would write this response's view back into
application state and recreate the staleness this avoids.

The list says whether it is the whole registry in two response headers, never
in the body, which stays the bare list its clients parse:

  - `X-Registry-Synced: true|false`: whether the on-chain mirror has finished
    a full pass since boot (`registry_sync.status()`). False means the list
    is a prefix of the registry still filling after a restart, and a count
    taken from it must not be cached or shown as the total.
  - `X-Registry-Count: <n>`: how many agents this response holds.

Without query parameters the list is the whole mirror, in the mirror's order,
exactly as it always was. Three optional parameters cut it down for a client
that does not want ~250 KB on every poll:

  - `limit` (1-1000) and `cursor` page through the registry ordered by id —
    keyset pagination, so a page boundary never shifts when agents are added
    or delisted between requests. `X-Next-Cursor` carries the cursor of the
    next page and is absent on the last; `X-Total-Count` is the size of the
    whole list either way. The cursor is opaque: pass back what was received.
  - `fields` (comma-separated) keeps only those keys of each agent; `id` is
    always kept. An unknown field name is a 422 naming the allowed ones.

Every answer carries an `ETag` and `Cache-Control: no-cache`: a client may
revalidate with `If-None-Match` and get a bodiless 304, but nothing may serve
the list without asking — `bound` changes the moment an operator binds, and
`X-Registry-Synced` the moment the mirror completes. The body is encoded once,
by the response itself, and gzipped by the app's middleware.
"""

import base64
import binascii
import re
from bisect import bisect_right
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import TypeAdapter
from pydantic_core import to_json

from .. import http_cache
from ..schemas import AGENT_ID_PATTERN, Agent
from ..security import CodedHTTPException
from ..services import registry_sync
from ..services.binding_registry import is_bound
from ..services.snapshots import etag_for
from ..state import state

REGISTRY_SYNCED_HEADER = "X-Registry-Synced"
REGISTRY_COUNT_HEADER = "X-Registry-Count"
TOTAL_COUNT_HEADER = "X-Total-Count"
NEXT_CURSOR_HEADER = "X-Next-Cursor"

# Page size bounds. A cursor without a limit pages by the default.
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100

AGENT_FIELDS = tuple(Agent.model_fields)
_AGENT_ID = re.compile(AGENT_ID_PATTERN)
_agent_list = TypeAdapter(list[Agent])

router = APIRouter(tags=["agents"])


def _with_bound(agent: Agent) -> Agent:
    """`agent` with `bound` answered for its provenance.

    A seeded agent is always None. It runs on a worker inside this process and
    has no endpoint to bind, so `False` would report a defect where there is
    none — the conflation the client's `needsBinding` exists to prevent. Only an
    on-chain agent can be meaningfully bound or unbound, and `is_bound` answers
    None for one of those too, when the bound set has not been loaded and we
    have nothing to report but our own ignorance.
    """
    bound = is_bound(agent.id) if agent.source == "onchain" else None
    return agent.model_copy(update={"bound": bound})


def encode_cursor(agent_id: str) -> str:
    """The opaque cursor for the page after `agent_id`."""
    return base64.urlsafe_b64encode(agent_id.encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> str:
    try:
        agent_id = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        agent_id = ""
    if not _AGENT_ID.fullmatch(agent_id):
        raise CodedHTTPException(400, "invalid_cursor", "cursor is not one this endpoint issued")
    return agent_id


def _selected_fields(fields: str | None) -> set[str] | None:
    """The keys `fields` asks for, `id` always among them; None for all."""
    if fields is None:
        return None
    wanted = {name.strip() for name in fields.split(",") if name.strip()}
    unknown = sorted(wanted - set(AGENT_FIELDS))
    if unknown or not wanted:
        raise CodedHTTPException(
            422,
            "invalid_fields",
            f"unknown field(s): {', '.join(unknown) or '(none given)'}; allowed: {', '.join(AGENT_FIELDS)}",
        )
    return wanted | {"id"}


def _page(agents: list[Agent], limit: int | None, cursor: str | None) -> tuple[list[Agent], str | None]:
    """One page by id after `cursor`, and the cursor of the next (None: last)."""
    size = limit or DEFAULT_PAGE_SIZE
    ordered = sorted(agents, key=lambda a: a.id)
    start = bisect_right([a.id for a in ordered], _decode_cursor(cursor)) if cursor is not None else 0
    page = ordered[start : start + size]
    more = start + size < len(ordered)
    return page, encode_cursor(page[-1].id) if more and page else None


@router.get(
    "/agents",
    response_model=list[Agent],
    summary="List registered agents",
    responses={304: {"description": "Not modified: the `If-None-Match` ETag is current."}},
)
async def list_agents(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE, description="Page size; pages by id.")] = None,
    cursor: Annotated[
        str | None, Query(min_length=1, max_length=64, description="`X-Next-Cursor` from the previous page.")
    ] = None,
    fields: Annotated[
        str | None, Query(min_length=1, max_length=512, description="Comma-separated keys to keep; `id` always kept.")
    ] = None,
) -> Response:
    """Every agent in the marketplace mirror. `X-Registry-Synced` says whether
    that mirror is complete; `X-Registry-Count` is the length of this list.

    With `limit`/`cursor`, one page of it by id (`X-Next-Cursor`,
    `X-Total-Count`); with `fields`, only those keys of each agent."""
    selected = _selected_fields(fields)
    # Status and list read with no await between them, so the header describes
    # exactly the list it rides on.
    synced = registry_sync.status().synced
    agents = state.list_agents()
    total = len(agents)
    next_cursor = None
    if limit is not None or cursor is not None:
        agents, next_cursor = _page(agents, limit, cursor)
    if selected is None:
        body = _agent_list.dump_json([_with_bound(a) for a in agents])
    else:
        stamp = "bound" in selected
        body = to_json([(_with_bound(a) if stamp else a).model_dump(include=selected, mode="json") for a in agents])
    headers = {
        REGISTRY_SYNCED_HEADER: "true" if synced else "false",
        REGISTRY_COUNT_HEADER: str(len(agents)),
        TOTAL_COUNT_HEADER: str(total),
    }
    if next_cursor is not None:
        headers[NEXT_CURSOR_HEADER] = next_cursor
    return http_cache.conditional_json(
        request,
        body=body,
        etag=etag_for(body),
        cache_control_value="no-cache",
        headers=headers,
    )


@router.get("/agents/{agent_id}", response_model=Agent, summary="Get one agent by id")
async def get_agent(agent_id: str) -> Agent:
    agent = state.agents.get(agent_id)
    if agent is None:
        raise HTTPException(404, f"unknown agent: {agent_id}")
    return _with_bound(agent)
