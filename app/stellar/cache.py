"""
Tiny TTL cache for read-only Soroban simulate calls.

FE polling hits the read routes with identical params every second or two;
each hit costs a full simulate round-trip to Soroban RPC. A short TTL cache
(seconds) collapses those into one upstream call without serving stale data
for longer than a poll interval.

Single-event-loop safe: entries are read/written only from the loop thread
(the producer may hop into a worker thread, but every dict mutation happens
back on the loop).

Concurrency model — single-flight with shield:
  - The first miss on a key spawns a real asyncio.Task for the producer and
    registers it per key; concurrent misses await the same flight.
  - Callers await the flight through asyncio.shield, so a cancelled caller
    (e.g. a batch read hitting its deadline) does NOT cancel the flight: the
    underlying work keeps running and its result still lands in the cache
    for the next request instead of being re-spawned from scratch.
  - Producer failures are negatively cached for a short window so a
    hard-down RPC doesn't fan out a fresh upstream call per request: within
    the window an equivalent exception is raised without spawning work.
  - `invalidate(key)` is how a caller says the upstream state changed. It
    drops the key's entry and failure and detaches its flight, and a per-key
    generation stops that flight — already reading the old state — from
    writing its outcome back, while its own callers still get their answer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

_store: dict[str, tuple[float, Any]] = {}

# In-flight producers: key → the Task computing that key's value.
_flights: dict[str, asyncio.Task[Any]] = {}

# Per-key generation, bumped by `invalidate`; absent means 0. A flight is
# spawned under its key's current generation and may write its outcome back
# only while that generation is still current. A read already in flight when
# the key was invalidated saw the upstream state from BEFORE the change that
# prompted the invalidation, and letting it land would quietly undo it.
_generations: dict[str, int] = {}

# Flights still running per key: the registered one plus any `invalidate`
# detached. This is what bounds `_generations`. A key's generation only has to
# outlive the flights that captured it, so both entries go the moment the last
# of them lands, and both maps are sized by the flights in progress rather than
# by every key ever invalidated. Restarting a generation at 0 then is safe
# precisely because nothing that captured an older value is left; dropping it
# while a stale flight still runs would let the restarted counter land back on
# that flight's number and wave its write through.
_running: dict[str, int] = {}

# Negative cache: key → (expiry, exception class, message). Hits within the
# window raise a FRESH instance — retaining live exception objects would pin
# their tracebacks (and captured frames) in memory, and re-raising the same
# object appends traceback frames on every hit.
_failures: dict[str, tuple[float, type[BaseException], str]] = {}
_NEGATIVE_TTL_SECONDS = 2.5

# Sweep threshold: entries are only overwritten, never evicted, so a stream of
# unique keys would grow the dicts forever. Past this combined size, misses
# sweep expired entries out. The gate counts _failures too: an attacker
# streaming unique failing keys (or a hard-down RPC) populates only the
# negative cache, and _store alone would never trip the sweep. (Flights
# self-clean via their done callbacks.)
_MAX_ENTRIES = 512

# How far below the cap an over-cap sweep evicts. Cache keys are caller-
# influenced (`agent:{id}`, `repstate:{id}`, `attestation:{hex}` — unbounded
# distinct values), so a stream of unique keys that are all still FRESH is
# reachable: expiry-only sweeping would free nothing, leaving both dicts to
# grow without bound AND making every later miss pay a full scan that
# reclaims nothing. Evicting to a low-water mark rather than exactly to the
# cap amortizes the ordering pass over the next `_EVICT_HEADROOM`
# admissions, so the fix cannot itself become the CPU cliff it prevents.
_EVICT_HEADROOM = 64


async def get_or_set(
    key: str,
    ttl_seconds: float,
    producer: Callable[[], Awaitable[Any]],
) -> Any:
    """Return the cached value for `key` if fresh, else await the producer.

    Successful results are cached for `ttl_seconds`. Failures are cached for
    `_NEGATIVE_TTL_SECONDS` and re-raised on hits within that window.
    """
    now = time.monotonic()
    hit = _store.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    neg = _failures.get(key)
    if neg is not None:
        expiry, exc_type, message = neg
        if expiry > now:
            raise _rebuild(exc_type, message)
        del _failures[key]
    task = _flights.get(key)
    if task is None or task.done():
        if len(_store) + len(_failures) > _MAX_ENTRIES:
            _sweep(now)
        # The generation is fixed when the flight is registered, not when it
        # first runs: a flight that `invalidate` detached is stale by
        # definition, whatever it goes on to read.
        task = asyncio.create_task(_produce(key, ttl_seconds, producer, _generations.get(key, 0)))
        _flights[key] = task
        _running[key] = _running.get(key, 0) + 1
        task.add_done_callback(partial(_on_flight_done, key))
    # shield: a cancelled caller must not cancel the shared flight — the
    # producer keeps running and its result still lands in the cache.
    return await asyncio.shield(task)


async def _produce(key: str, ttl_seconds: float, producer: Callable[[], Awaitable[Any]], generation: int) -> Any:
    try:
        value = await producer()
    except Exception as e:
        # A stale failure is fenced too: negatively cached, it would refuse the
        # first reader after an invalidation with an error from before it.
        if _is_current(key, generation):
            _failures[key] = (time.monotonic() + _NEGATIVE_TTL_SECONDS, type(e), str(e))
        raise
    if _is_current(key, generation):
        _store[key] = (time.monotonic() + ttl_seconds, value)
    return value


def _is_current(key: str, generation: int) -> bool:
    """Whether a flight spawned under `generation` may still write `key` back.

    Checked at the write itself, with no await between check and store, so on
    one event loop nothing can invalidate the key in between. A stale flight
    still RETURNS its outcome to the callers already awaiting it — they asked
    before the change and get the answer that was true then; it just stops
    being the cache's answer for everyone after.
    """
    return _generations.get(key, 0) == generation


def _rebuild(exc_type: type[BaseException], message: str) -> BaseException:
    """Fresh exception instance for a negative-cache hit.

    Preserves the original class when it's constructible from its message
    (the common case); classes with other constructor signatures fall back to
    a RuntimeError naming the original type.
    """
    try:
        return exc_type(message)
    except Exception:
        return RuntimeError(f"{exc_type.__name__}: {message}")


def _on_flight_done(key: str, task: asyncio.Task[Any]) -> None:
    if _flights.get(key) is task:
        del _flights[key]
    remaining = _running.get(key, 0) - 1
    if remaining > 0:
        _running[key] = remaining
    else:
        _running.pop(key, None)
        _generations.pop(key, None)
    if not task.cancelled():
        # Mark a failure as retrieved even if every caller was cancelled
        # before it landed; the exception lives on in the negative cache.
        task.exception()


def _sweep(now: float) -> None:
    """Reclaim space: expired entries first, then, if the combined size is
    still over the cap, the entries closest to expiring.

    The second pass is what makes the cache actually bounded. Expiry order is
    the right eviction key here and needs no extra bookkeeping: within one TTL
    class the smallest expiry IS the oldest write, and across classes it
    prefers dropping whatever has the least remaining life — a 3 s read that
    is about to lapse anyway goes before a 15 s one just written.

    Evicting an entry with a live flight is harmless: the producer writes its
    result back on completion.
    """
    for k in [k for k, (exp, _) in _store.items() if exp <= now]:
        del _store[k]
    for k in [k for k, entry in _failures.items() if entry[0] <= now]:
        del _failures[k]

    overflow = len(_store) + len(_failures) - max(_MAX_ENTRIES - _EVICT_HEADROOM, 0)
    if overflow <= 0:
        return
    # One ordering over both dicts, so the cap is enforced on their combined
    # size rather than on each independently. The middle element tags which
    # dict a key came from (and keeps the sort total for equal expiries).
    victims = sorted(
        [(exp, 0, k) for k, (exp, _) in _store.items()] + [(entry[0], 1, k) for k, entry in _failures.items()]
    )[:overflow]
    for _expiry, in_failures, key in victims:
        if in_failures:
            _failures.pop(key, None)
        else:
            _store.pop(key, None)


def invalidate(key: str) -> None:
    """Forget `key` now, so the next read of it goes upstream.

    For upstream state that changed under the cache — a rating that just
    landed moves an agent's score, and serving the old value for the rest of
    its TTL is exactly what a caller acting on the change cannot have.
    Dropping the stored entry is the easy half. A read already in flight when
    the state changed is the hard half, and it is:

      - DETACHED from `_flights`, so a caller arriving after this point spawns
        a fresh read instead of joining one that started too early;
      - NOT cancelled, so the callers already awaiting it still get their
        answer (the shield in `get_or_set` keeps it running regardless);
      - unable to write back, because its captured generation is no longer
        current (`_is_current`).

    The failure cache goes too: an error from before the change says nothing
    about the state after it.
    """
    _store.pop(key, None)
    _failures.pop(key, None)
    _flights.pop(key, None)
    if key in _running:
        # Only a running flight can write back, so only then is there anything
        # to fence. With none, recording a generation would only grow the map:
        # the next flight is spawned after this call and is fresh by
        # construction.
        _generations[key] = _generations.get(key, 0) + 1


def clear() -> None:
    """Drop all cached entries, failures, and flight registrations (tests).

    Every key is invalidated at once, so a flight still running is fenced
    exactly as `invalidate` fences one: its generation is bumped and it cannot
    write into the emptied cache. `_running` is deliberately kept — those
    tasks still exist and their done callbacks will retire them, whereas
    zeroing the counts (or the generations) would let a pre-clear read land
    after the clear, or a finishing flight retire a count that belongs to a
    newer one.
    """
    _store.clear()
    _failures.clear()
    _flights.clear()
    for key in _running:
        _generations[key] = _generations.get(key, 0) + 1
