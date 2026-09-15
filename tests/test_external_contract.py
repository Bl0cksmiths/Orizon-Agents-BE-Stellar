"""The operator response contract — ADR 0004 D1.

What this suite is actually pinning: that an operator cannot put a key of their
choosing into `context[worker.name]`, and therefore cannot reach billing,
on-chain reputation, the SSE trace or the buyer's viewer with anything we did
not name in advance. Almost every test below is the same assertion wearing a
different hat — *this did not survive* — so the negative cases are the point
and the happy path is the control.

Hostile input is spelled out literally rather than generated. A fuzz case that
passes tells you nothing about which shape it tried; `{"artifact": "nope"}`
written out tells the next reader exactly what the operator did and what we did
about it.

The module is pure and synchronous, so there is no `asyncio.run` here. (The
repo has no pytest-asyncio; where a test does need a coroutine, a bare
`asyncio.run` inside a sync test is the house idiom — see
tests/test_endpoint_policy.py.)
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents.workers.external_contract import (
    OUTPUT_RULES,
    ExternalOutputError,
    parse_operator_output,
)

# A well-formed response, every optional field populated. Tests mutate a copy of
# this so a failure reads as "this one field went wrong", not "this whole
# unfamiliar blob was rejected".
GOOD: dict[str, Any] = {
    "summary": "Refactored the checkout flow and shipped a preview",
    "artifact": {
        "title": "Checkout v2",
        "files": [{"path": "index.html", "content": "<html><head></head><body>hi</body></html>"}],
        "preview_html": "<html><head></head><body>hi</body></html>",
    },
    "critic_violations": [],
    "critic_notes": ["tightened the empty state"],
    "preview_url": "https://operator.example/preview/abc",
}


def _response(**overrides: Any) -> dict[str, Any]:
    """`GOOD` with `overrides` applied, deep-copied so tests cannot leak state."""
    body = json.loads(json.dumps(GOOD))
    body.update(overrides)
    return body


def test_a_well_formed_response_survives_intact() -> None:
    out = parse_operator_output(_response())

    assert out["summary"] == GOOD["summary"]
    assert out["artifact"]["title"] == "Checkout v2"
    assert [f["path"] for f in out["artifact"]["files"]] == ["index.html"]
    assert out["critic_violations"] == []
    assert out["critic_notes"] == ["tightened the empty state"]
    assert out["preview_url"] == GOOD["preview_url"]
    # The exact key set, because "what else came back" is the whole question.
    assert set(out) == {"summary", "artifact", "critic_violations", "critic_notes", "preview_url"}


def test_no_operator_container_is_passed_through() -> None:
    # The allowlist is only worth anything if it COPIES. A returned reference to
    # the operator's own list or dict would put their object in `context`, where
    # a later mutation of it is a mutation of ours.
    raw = _response()
    out = parse_operator_output(raw)

    assert out is not raw
    assert out["artifact"] is not raw["artifact"]
    assert out["artifact"]["files"] is not raw["artifact"]["files"]
    assert out["artifact"]["files"][0] is not raw["artifact"]["files"][0]
    assert out["critic_notes"] is not raw["critic_notes"]

    raw["artifact"]["files"][0]["path"] = "mutated-after-the-fact.html"
    assert out["artifact"]["files"][0]["path"] == "index.html"


# (response, the rule that must refuse it). A module constant rather than an
# inline decorator argument so the coverage test below can prove that every rule
# in the vocabulary is actually reachable.
_REFUSALS: list[tuple[Any, str]] = [
    # not_an_object — the body decoded, but not into a worker output.
    ("just a string", "not_an_object"),
    ([{"summary": "a list of one good response is still a list"}], "not_an_object"),
    (42, "not_an_object"),
    (None, "not_an_object"),  # JSON `null` decodes to None, and None is not a response
    (True, "not_an_object"),
    # summary_missing — the step reported no outcome.
    ({}, "summary_missing"),
    ({"artifact": {"title": "shipped"}}, "summary_missing"),  # an artifact is not a report
    ({"summary": ""}, "summary_missing"),
    ({"summary": "   \n\t "}, "summary_missing"),  # whitespace is not a summary
    ({"summary": 5}, "summary_missing"),
    ({"summary": None}, "summary_missing"),
    ({"summary": ["a", "b"]}, "summary_missing"),
    ({"summary": {"text": "done"}}, "summary_missing"),
    # artifact_not_an_object — the deliverable arrived as something else.
    ({"summary": "ok", "artifact": "<html>the whole document as a string</html>"}, "artifact_not_an_object"),
    ({"summary": "ok", "artifact": [{"path": "a", "content": "b"}]}, "artifact_not_an_object"),
    ({"summary": "ok", "artifact": 7}, "artifact_not_an_object"),
    ({"summary": "ok", "artifact": True}, "artifact_not_an_object"),
]


@pytest.mark.parametrize(("raw", "rule"), _REFUSALS)
def test_each_refusal_names_its_rule(raw: Any, rule: str) -> None:
    # `.rule` is the assertable part. The message stays free to be useful to a
    # human without a test pinning its wording.
    with pytest.raises(ExternalOutputError) as exc:
        parse_operator_output(raw)
    assert exc.value.rule == rule
    assert str(exc.value)  # and it still says something


def test_every_rule_is_exercised() -> None:
    # A rule nobody can produce is a dead error code that a caller will
    # nonetheless write a branch for.
    assert {rule for _, rule in _REFUSALS} == OUTPUT_RULES


def test_rule_vocabulary_is_closed() -> None:
    # A typo at a raise site must fail here, loudly, rather than travel to a
    # caller as an error code nothing handles — so the constructor raises a
    # PLAIN ValueError, which is deliberately not catchable as a refusal.
    with pytest.raises(ValueError) as exc:
        ExternalOutputError("summary_to_long", "typo at the raise site")
    assert not isinstance(exc.value, ExternalOutputError)
    assert "summary_to_long" in str(exc.value)


def test_refusal_is_a_value_error() -> None:
    # Callers that do not care which rule fired still get a sane except clause.
    with pytest.raises(ValueError):
        parse_operator_output({"summary": ""})
