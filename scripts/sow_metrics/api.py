"""HTTP reads of the live deployment: the API, the backend host, the frontend pages and GitHub.

Every call is a GET, retried a bounded number of times on a transport error
or a transient status. Nothing is ever posted.

A non-200 answer is returned, not raised: "the guide answers 404" is a
finding (a build that predates the page), not an error in the generator. Only
a service that never gave an answer at all raises `Unreachable`, and the
metric that needed it is then reported "Not measured" — never 0, never met.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .retry import RETRYABLE_STATUS, RetryableStatus, RetryPolicy, retry_after_seconds

GET_TIMEOUT = 60.0
USER_AGENT = "orizon-sow-metrics/1.0 (read-only)"


class Unreachable(Exception):
    """The service gave no answer at all, after the bounded retries."""


@dataclass(frozen=True)
class Answer:
    url: str
    status: int
    body: Any  # the parsed JSON, or None when the body is not JSON
    text: str = ""
    location: str | None = None  # a redirect's target, for a page that is not a 200

    @property
    def ok(self) -> bool:
        return self.status == 200

    def obj(self) -> dict[str, Any]:
        """The body when it is a JSON object, else an empty dict."""
        return self.body if isinstance(self.body, dict) else {}


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


@dataclass
class Reads:
    client: httpx.Client
    api: str  # e.g. https://orizons.xyz (the /api prefix is added here)
    backend: str  # the backend's own host, for /readiness and /openapi.json
    frontend: str
    github_api: str
    retry: RetryPolicy

    def get(
        self,
        url: str,
        *,
        follow_redirects: bool = True,
        answers: frozenset[int] = frozenset(),
        accept: str = "application/json",
    ) -> Answer:
        """One GET, retried on transient failures. Any final status is an Answer.

        `answers` names statuses that are a real answer on this route rather
        than a transient failure: /readiness answers 503 WITH its report when
        a dependency is missing, and asking again gets the same report.
        """

        def once() -> Answer:
            response = self.client.get(
                url,
                timeout=GET_TIMEOUT,
                headers={"accept": accept, "user-agent": USER_AGENT},
                follow_redirects=follow_redirects,
            )
            if response.status_code in RETRYABLE_STATUS and response.status_code not in answers:
                raise RetryableStatus(response.status_code, retry_after_seconds(response))
            return Answer(url, response.status_code, _json(response), response.text, response.headers.get("location"))

        try:
            return self.retry.run(once)
        except RetryableStatus as exc:
            raise Unreachable(f"GET {url} kept answering HTTP {exc.status} after retries") from exc
        except httpx.HTTPError as exc:
            raise Unreachable(f"GET {url} gave no answer after retries: {type(exc).__name__}: {exc}") from exc

    def api_url(self, path: str) -> str:
        return f"{self.api}/api{path}"

    # ── the deployment ──────────────────────────────────────────
    def network(self) -> Answer:
        return self.get(self.api_url("/stellar/network"))

    def readiness(self) -> Answer:
        return self.get(f"{self.backend}/readiness", answers=frozenset({503}))

    def adoption(self) -> Answer:
        """The live adoption report: read here only for each agent's `bound` flag."""
        return self.get(self.api_url("/ecosystem/adoption"))

    def reputation_params(self) -> Answer:
        return self.get(self.api_url("/stellar/reputation/params"))

    def openapi(self) -> Answer:
        return self.get(f"{self.backend}/openapi.json")

    def page(self, path: str) -> Answer:
        """A frontend page, NOT following redirects: a redirect to a login is not a published page."""
        return self.get(f"{self.frontend}{path}", follow_redirects=False, accept="text/html")

    def page_url(self, path: str) -> str:
        return f"{self.frontend}{path}"

    # ── GitHub ──────────────────────────────────────────────────
    def repository(self, full_name: str) -> Answer:
        """`GET /repos/{owner}/{repo}`, unauthenticated: 404 is an answer (no such public repository)."""
        return self.get(f"{self.github_api}/repos/{full_name}", accept="application/vnd.github+json")
