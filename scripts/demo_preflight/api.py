"""HTTP reads of the deployment: the API behind `--api`, the backend host, and the frontend pages.

Every call here is a GET, retried a bounded number of times on a transport
error or a transient status, except one: `decompose`, which is a POST that
stores a plan and costs a model call. It is made only under
`--with-decompose`, exactly once, and never retried — a retried write is a
second plan.

A non-200 answer is returned, not raised: "the route answered 404" is the
finding a check reports (a build that predates the route), not an error in the
pre-flight. Only a service that never gave an answer at all raises
`Unreachable`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .retry import RETRYABLE_STATUS, RetryableStatus, RetryPolicy, retry_after_seconds

GET_TIMEOUT = 60.0
# A decompose calls the planning model; the backend's own bound is well under this.
DECOMPOSE_TIMEOUT = 150.0
HEALTH_ATTEMPT_TIMEOUT = 30.0


class Unreachable(Exception):
    """The service gave no answer at all, after the bounded retries."""


@dataclass(frozen=True)
class Answer:
    url: str
    status: int
    body: Any  # the parsed JSON, or None when the body is not JSON
    location: str | None = None  # a redirect's target, for a page that is not a 200

    @property
    def ok(self) -> bool:
        return self.status == 200

    def obj(self) -> dict[str, Any]:
        """The body when it is a JSON object, else an empty dict."""
        return self.body if isinstance(self.body, dict) else {}


@dataclass(frozen=True)
class Warmup:
    ok: bool
    seconds: float  # from the first probe to the first 200 (or to giving up)
    attempts: int
    last_error: str | None


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


@dataclass
class Reads:
    client: httpx.Client
    api: str  # e.g. https://orizons.xyz (the /api prefix is added here)
    backend: str  # the backend's own host, for the root-level /readiness
    frontend: str
    retry: RetryPolicy
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    # ── plumbing ────────────────────────────────────────────────
    def get(self, url: str, *, follow_redirects: bool = True, answers: frozenset[int] = frozenset()) -> Answer:
        """One GET, retried on transient failures. Any final status is an Answer.

        `answers` names statuses that are a real answer on this route rather
        than a transient failure: /readiness answers 503 WITH its report when
        a dependency is missing, and asking again gets the same report.
        """

        def once() -> Answer:
            response = self.client.get(
                url, timeout=GET_TIMEOUT, headers={"accept": "application/json"}, follow_redirects=follow_redirects
            )
            if response.status_code in RETRYABLE_STATUS and response.status_code not in answers:
                raise RetryableStatus(response.status_code, retry_after_seconds(response))
            return Answer(url, response.status_code, _json(response), response.headers.get("location"))

        try:
            return self.retry.run(once)
        except RetryableStatus as exc:
            return Answer(url, exc.status, None)
        except httpx.HTTPError as exc:
            raise Unreachable(f"GET {url} gave no answer after retries: {type(exc).__name__}: {exc}") from exc

    def api_url(self, path: str) -> str:
        return f"{self.api}/api{path}"

    # ── the backend waking up ───────────────────────────────────
    def warm(self, budget: float) -> Warmup:
        """GET /api/health until it answers 200 or `budget` seconds pass.

        The way components/backend-warmup.tsx wakes the service, but waited
        on: Render's free tier sleeps, and the recording must not be the
        request that pays for the boot. Each probe is one attempt; the loop
        is the retry, bounded by the budget.
        """
        url = self.api_url("/health")
        start = self.clock()
        deadline = start + budget
        delay = 2.0
        attempts = 0
        last_error: str | None = None
        while True:
            attempts += 1
            try:
                response = self.client.get(url, timeout=HEALTH_ATTEMPT_TIMEOUT, follow_redirects=True)
                if response.status_code == 200:
                    return Warmup(True, self.clock() - start, attempts, None)
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if self.clock() + delay > deadline:
                return Warmup(False, self.clock() - start, attempts, last_error)
            self.sleep(delay)
            delay = min(delay * 2, 10.0)

    # ── reads ───────────────────────────────────────────────────
    def network(self) -> Answer:
        return self.get(self.api_url("/stellar/network"))

    def readiness(self) -> Answer:
        return self.get(f"{self.backend}/readiness", answers=frozenset({503}))

    def adoption(self) -> Answer:
        return self.get(self.api_url("/ecosystem/adoption"))

    def agent_readiness(self, agent_id: str) -> Answer:
        return self.get(self.api_url(f"/agents/{agent_id}/readiness"))

    def agents(self) -> Answer:
        return self.get(self.api_url("/agents"))

    def reputation(self) -> Answer:
        return self.get(self.api_url("/stellar/reputation"))

    def reputation_params(self) -> Answer:
        return self.get(self.api_url("/stellar/reputation/params"))

    def page(self, path: str) -> Answer:
        """A frontend page, NOT following redirects: a redirect to a login is not a 200."""
        return self.get(f"{self.frontend}{path}", follow_redirects=False)

    # ── the one write, opt-in ───────────────────────────────────
    def decompose(self, intent: str) -> Answer:
        """POST /api/orchestrator/decompose, once. Stores a plan; costs a model call."""
        url = self.api_url("/orchestrator/decompose")
        try:
            response = self.client.post(url, json={"intent": intent}, timeout=DECOMPOSE_TIMEOUT)
        except httpx.HTTPError as exc:
            raise Unreachable(f"POST {url} gave no answer: {type(exc).__name__}: {exc}") from exc
        return Answer(url, response.status_code, _json(response))
