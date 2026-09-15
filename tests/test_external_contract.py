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
    MAX_ARTIFACT_CHARS,
    MAX_FILES,
    MAX_NOTE_CHARS,
    MAX_NOTES,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
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


@pytest.mark.parametrize(
    "files",
    [
        "index.html",  # the whole file list as one string
        {"index.html": "<html></html>"},  # a mapping instead of a list
        7,
        None,
    ],
)
def test_a_malformed_file_list_is_dropped_not_refused(files: Any) -> None:
    # The artifact envelope's TYPE is a refusal; a field inside it is a drop.
    # The summary and the preview still describe work that was really done, and
    # failing the step here would un-bill it over a container mistake.
    out = parse_operator_output(_response(artifact={"title": "Checkout v2", "files": files}))
    assert out["artifact"] == {"title": "Checkout v2"}


def test_non_dict_file_entries_are_skipped() -> None:
    # Unlike a critic list, a file list is filtered per entry: a shorter list of
    # files is not a claim about the work, so dropping the junk keeps what is
    # real. "index.html" survives; nothing else does.
    out = parse_operator_output(
        _response(
            artifact={
                "files": [
                    "index.html",
                    42,
                    None,
                    ["index.html", "<html></html>"],
                    {"path": "orphan.html"},  # a path with no bytes is not a file
                    {"path": "", "content": "<html></html>"},  # nor is content with no path
                    {"path": "typed.html", "content": 12345},  # nor are non-string bytes
                    {"path": "index.html", "content": "<html><body>real</body></html>"},
                ]
            }
        )
    )
    assert [f["path"] for f in out["artifact"]["files"]] == ["index.html"]
    assert out["artifact"]["files"][0]["content"].endswith("<body>real</body></html>")


def test_the_file_list_is_capped() -> None:
    files = [{"path": f"f{i}.txt", "content": "x"} for i in range(MAX_FILES * 3)]
    out = parse_operator_output(_response(artifact={"files": files}))
    assert len(out["artifact"]["files"]) == MAX_FILES
    # Truncated from the front, so the cap is a cap and not a reshuffle.
    assert out["artifact"]["files"][-1]["path"] == f"f{MAX_FILES - 1}.txt"


def test_unknown_artifact_keys_are_dropped() -> None:
    out = parse_operator_output(
        _response(
            artifact={
                "title": "Checkout v2",
                "entry": "index.html",
                "language": "html",
                "source": "baked",
                "download_url": "https://operator.example/zip",
                "__proto__": {"admin": True},
            }
        )
    )
    assert out["artifact"] == {"title": "Checkout v2"}


def test_an_artifact_with_nothing_in_it_does_not_survive() -> None:
    # `.get("artifact")` must stay falsy: it is what the trace branches on and
    # what synthetic_rating pays +15 for. An empty dict claiming delivery would
    # be a rating lever made of nothing.
    for empty in ({}, {"unknown": "key"}, {"title": ""}, {"title": 9, "files": "no"}):
        out = parse_operator_output(_response(artifact=empty))
        assert "artifact" not in out


def test_a_null_artifact_is_absent_not_malformed() -> None:
    # JSON `null` is how an operator says "no artifact this step" — deploy_v0
    # spells preview_url exactly that way. Refusing it would fail a step for
    # declining to attach something optional.
    out = parse_operator_output(_response(artifact=None))
    assert "artifact" not in out
    assert out["summary"] == GOOD["summary"]


_CRITIC_KEYS = ("critic_violations", "critic_notes")

_MALFORMED_NOTE_LISTS: list[Any] = [
    [1, 2],  # the card's case: numbers where strings belong
    {"0": "first"},  # a mapping instead of a list
    "a single note, unwrapped",
    7,
    [None],
    [["nested"]],
    [{"text": "structured note"}],
    ["fine", 2, "also fine"],  # one bad item spoils the list — see below
]


@pytest.mark.parametrize("key", _CRITIC_KEYS)
@pytest.mark.parametrize("value", _MALFORMED_NOTE_LISTS)
def test_a_malformed_critic_list_is_dropped_whole(key: str, value: Any) -> None:
    # Dropped WHOLE, never filtered down to the well-typed items — and this is
    # the assertion that matters most in this file. reputation_svc does:
    #
    #     if isinstance(violations, list):
    #         rating += 10 if not violations else -3 * min(len(violations), 10)
    #
    # so `[1, 2]` filtered to `[]` would not be a tidied field, it would be a
    # +10 rating bonus minted out of a malformed one, settled on-chain. Absent
    # is not a list, so the branch never runs and nothing is awarded.
    out = parse_operator_output(_response(**{key: value}))
    assert key not in out
    assert out.get(key) is None
    # …and it is a drop, not a refusal: the step still delivered.
    assert out["summary"] == GOOD["summary"]


@pytest.mark.parametrize("key", _CRITIC_KEYS)
def test_an_empty_critic_list_is_honoured(key: str) -> None:
    # "The critic found nothing" is a claim we cannot verify, and we take it at
    # face value exactly as the local path does for our own workers. What the
    # rule above refuses is MANUFACTURING that claim from input that never
    # made it.
    out = parse_operator_output(_response(**{key: []}))
    assert out[key] == []


@pytest.mark.parametrize("key", _CRITIC_KEYS)
def test_critic_notes_are_capped_and_clamped(key: str) -> None:
    out = parse_operator_output(_response(**{key: ["n" * (MAX_NOTE_CHARS * 4)] * (MAX_NOTES * 5)}))
    assert len(out[key]) == MAX_NOTES
    assert all(len(note) == MAX_NOTE_CHARS for note in out[key])
    assert all(note.endswith("[truncated]") for note in out[key])


def test_empty_strings_inside_a_critic_list_are_kept() -> None:
    # Dropping them would shorten a violations list, and shorter means a
    # smaller penalty. Every filter on this field points one way; none of them
    # may point the operator's way.
    out = parse_operator_output(_response(critic_violations=["", "  ", "real violation"]))
    assert out["critic_violations"] == ["", "  ", "real violation"]


# Slack over the raw ceiling that a clamped HTML payload is allowed to carry:
# code_gen's truncation note, the `</script>`/`</body>`/`</html>` it re-appends
# so the cut document still parses, and the CSP <meta> injected afterwards.
_CLAMP_SLACK = 1_024


def test_the_artifact_ceiling_is_the_local_one() -> None:
    # Imported, not restated: external output must not be allowed more room
    # than our own workers get, and a bound with two copies drifts.
    from app.agents.workers.code_gen import MAX_ARTIFACT_CHARS as local_ceiling

    assert MAX_ARTIFACT_CHARS == local_ceiling


def test_an_oversize_summary_is_clamped() -> None:
    out = parse_operator_output(_response(summary="s" * (MAX_SUMMARY_CHARS * 10)))
    assert len(out["summary"]) == MAX_SUMMARY_CHARS
    assert out["summary"].endswith("[truncated]")


def test_an_oversize_title_is_clamped() -> None:
    out = parse_operator_output(_response(artifact={"title": "t" * (MAX_TITLE_CHARS * 10)}))
    assert len(out["artifact"]["title"]) == MAX_TITLE_CHARS
    assert out["artifact"]["title"].endswith("[truncated]")


def test_oversize_html_payloads_are_clamped() -> None:
    # A megabyte of "artifact" is not an artifact: it is re-sent to every later
    # operator, held in state for 200 tasks, and streamed to the viewer.
    huge = "<html><head></head><body>" + ("z" * (MAX_ARTIFACT_CHARS * 2))
    artifact = {"files": [{"path": "big.html", "content": huge}], "preview_html": huge}
    out = parse_operator_output(_response(artifact=artifact))

    preview = out["artifact"]["preview_html"]
    content = out["artifact"]["files"][0]["content"]
    for payload in (preview, content):
        assert len(payload) < len(huge)
        assert len(payload) <= MAX_ARTIFACT_CHARS + _CLAMP_SLACK
        # code_gen's clamp, reused — it leaves its own mark and re-closes the
        # document so the truncated HTML still parses in the iframe.
        assert "orizon: artifact truncated" in payload
        assert payload.rstrip().endswith("</html>")


def test_an_oversize_file_path_is_clamped_not_dropped() -> None:
    out = parse_operator_output(_response(artifact={"files": [{"path": "p" * 5_000, "content": "x"}]}))
    assert len(out["artifact"]["files"][0]["path"]) == 200  # ArtifactFile.path's max_length
