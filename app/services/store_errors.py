"""A Postgres store that cannot be reached, said as one error rather than many.

Every durable store here talks to the same Postgres, and when that database is
gone — a Neon project over its compute quota, a cold endpoint refusing
connections, a socket that died while the instance idled — each store used to
let asyncpg's own exception out. Nothing above it knew those apart from a bug,
so a buyer reading their dispute and an operator reading their binding were
both answered 500 `internal_error`: "we broke", when the truth was "the record
store is down, ask again shortly".

`StoreUnavailable` is that truth. A store's Postgres methods are wrapped in
`outage_boundary`, which turns a CONNECTION failure — and only that — into this
one exception; `app/main.py` answers it 503 `store_unavailable` with a
Retry-After. Everything else passes through untouched: a syntax error, a
constraint violation or a value the column refuses is a defect in this
service, and a 503 would invite a client to retry a request that can never
succeed. The line is drawn by exception class, never by message, with one
exception noted where it is made.

The driver is never imported here. asyncpg is optional (the stores import it
lazily, at first Postgres use), and an asyncpg exception can only exist once
asyncpg has been imported — so the classes are looked up on the module already
loaded, and a process without the driver simply has none to match.
"""

from __future__ import annotations

import contextlib
import functools
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

# asyncpg's server-side errors that mean "no usable connection", by class:
# SQLSTATE class 08 (PostgresConnectionError, whose subclasses include the
# ConnectionDoesNotExistError a dead pooled connection raises), class 53
# (InsufficientResourcesError and its TooManyConnectionsError — what an
# exhausted Neon quota reaches the driver as), and three of class 57's
# operator interventions: the server refusing new connections, or shutting
# down under the session. NOT class 57 as a whole: QueryCanceledError (57014)
# is a statement timeout, which says something about the query.
_OUTAGE_CLASSES = (
    "PostgresConnectionError",
    "InsufficientResourcesError",
    "CannotConnectNowError",
    "AdminShutdownError",
    "CrashShutdownError",
)

# asyncpg raises a bare InterfaceError both for a connection that is closed —
# an outage — and for misuse such as two operations on one connection — a bug.
# Only the message tells them apart, so this is the one place a message is
# read: "connection is closed", "pool is closing", "pool is closed".
_CLOSED_MARKERS = ("is closed", "is closing")


class StoreUnavailable(Exception):
    """A store could not reach its database; the request may succeed later.

    `store` is a generic, human name ("dispute record") and is what the 503's
    message says. `cause` is the exception that was translated, kept for the
    log by its CLASS only: a driver's message can carry a host and port, and
    neither belongs in a response or in a line a support engineer pastes.
    """

    def __init__(self, store: str, cause: BaseException) -> None:
        super().__init__(f"the {store} store is unavailable ({type(cause).__name__})")
        self.store = store
        self.cause = cause


def is_connection_failure(exc: BaseException) -> bool:
    """Whether `exc` means the database could not be reached, not that a query was wrong.

    OSError covers the transport: a refused or reset socket, an unresolvable
    host, and — since TimeoutError is an OSError — a connect, an acquire or a
    command that ran out of time. A command timeout cannot be told apart from
    a connect timeout at `pool.fetchrow`, and the stores' bounds are set so that
    anything reaching one is a database in trouble rather than a slow query.
    """
    if isinstance(exc, OSError):
        return True
    asyncpg = sys.modules.get("asyncpg")
    if asyncpg is None:
        return False
    if isinstance(exc, tuple(getattr(asyncpg, name) for name in _OUTAGE_CLASSES)):
        return True
    if isinstance(exc, asyncpg.InterfaceError):
        text = str(exc)
        return any(marker in text for marker in _CLOSED_MARKERS)
    return False


@contextlib.asynccontextmanager
async def store_boundary(store: str) -> AsyncIterator[None]:
    """Translate a connection failure inside the block into `StoreUnavailable`."""
    try:
        yield
    except Exception as exc:
        if not is_connection_failure(exc):
            raise
        raise StoreUnavailable(store, exc) from exc


def outage_boundary(
    store: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """Decorate a store's async method so its connection failures leave as `StoreUnavailable`.

    The whole method is enclosed, not just the pool's creation: with
    `min_size=0` a pool dials on demand, so the connect that fails is usually
    the one inside `pool.fetchrow(...)`, long after the pool was made.
    """

    def wrap(method: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
        @functools.wraps(method)
        async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            async with store_boundary(store):
                return await method(*args, **kwargs)

        return guarded

    return wrap
