"""The adoption verifier waits out a 202 "computing" answer — as told, and bounded.

Since D-091 the endpoint answers 202 with Retry-After while the first report
since a boot is computed. The verifier must not read that as a failure, must
wait what Retry-After says (clamped), and must give up, saying so, once its
total budget is spent.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from scripts.adoption_report import config
from scripts.adoption_report.api import AdoptionApi, ApiUnreachable
from scripts.adoption_report.retry import RetryPolicy

BASE = "https://api.test"
REPORT = {"network": "testnet", "totals": {}}


def _api(answers: list[httpx.Response], slept: list[float], **kw: Any) -> tuple[AdoptionApi, list[str]]:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return answers.pop(0) if len(answers) > 1 else answers[0]

    client = httpx.Client(transport=httpx.MockTransport(handle))
    return AdoptionApi(client=client, base=BASE, retry=RetryPolicy(sleep=slept.append), **kw), asked


def _computing(retry_after: str | None = "30") -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    body = {"status": "computing", "message": "being computed", "retry_after_seconds": 30}
    return httpx.Response(202, json=body, headers=headers)


def test_a_202_is_waited_out_as_retry_after_says_then_the_report_is_read() -> None:
    slept: list[float] = []
    api, asked = _api([_computing("30"), _computing("12"), httpx.Response(200, json=REPORT)], slept)

    assert api.fetch() == REPORT
    assert slept == [30.0, 12.0]
    assert len(asked) == 3


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, config.PENDING_DEFAULT_DELAY_SECONDS),
        ("0", config.PENDING_MIN_DELAY_SECONDS),
        ("3600", config.PENDING_MAX_DELAY_SECONDS),
        ("soon", config.PENDING_DEFAULT_DELAY_SECONDS),
    ],
)
def test_the_wait_between_asks_is_clamped(header: str | None, expected: float) -> None:
    slept: list[float] = []
    api, _ = _api([_computing(header), httpx.Response(200, json=REPORT)], slept)

    api.fetch()
    assert slept == [expected]


def test_the_total_wait_is_bounded_and_giving_up_says_why() -> None:
    slept: list[float] = []
    api, asked = _api([_computing("30")], slept, pending_max_wait_seconds=75.0)

    with pytest.raises(ApiUnreachable, match=r"still computing the report after 75 s"):
        api.fetch()
    assert slept == [30.0, 30.0, 15.0]  # the last wait is cut to what the budget has left
    assert sum(slept) == 75.0
    assert len(asked) == 4


def test_a_202_after_a_transient_503_is_still_waited_for() -> None:
    slept: list[float] = []
    unavailable = httpx.Response(503, json={"error": "no"}, headers={"Retry-After": "2"})
    api, _ = _api([unavailable, _computing("5"), httpx.Response(200, json=REPORT)], slept)

    assert api.fetch() == REPORT
    assert slept == [2.0, 5.0]


def test_any_other_status_is_still_an_answer_not_a_wait() -> None:
    slept: list[float] = []
    api, _ = _api([httpx.Response(404, json={"error": "no"})], slept)

    with pytest.raises(ApiUnreachable, match="HTTP 404"):
        api.fetch()
    assert slept == []
