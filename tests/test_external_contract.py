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

from app.agents.workers.external_contract import parse_operator_output

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
