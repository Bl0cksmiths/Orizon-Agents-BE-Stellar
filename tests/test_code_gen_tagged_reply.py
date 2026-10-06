"""code.gen's tagged reply: raw HTML between tags, read back into a CodeArtifact.

On Claude, code.gen and code.critic answer as three tagged sections rather
than CodeArtifact JSON (`TAGGED_SHAPE`), so a whole app is never escaped into
a JSON string. These pin how a reply is read: what counts as the HTML, where
the title and summary may come from, and what a reply without an app does.
"""

from __future__ import annotations

import json

import pytest

from app.agents.workers.code_gen import (
    CLAUDE_INSTRUCTIONS,
    CLAUDE_UPSTREAM,
    INSTRUCTIONS,
    MAX_ARTIFACT_CHARS,
    TAGGED_SHAPE,
    parse_tagged_artifact,
)

HTML = "<!doctype html>\n<html><head><title>T</title></head><body><main>hi</main></body></html>"


def _reply(title: str = "Pomodoro Pro", summary: str = "A focused timer.", html: str = HTML) -> str:
    return (
        f"<artifact_title>{title}</artifact_title>\n"
        f"<artifact_summary>{summary}</artifact_summary>\n"
        f"<artifact_html>\n{html}\n</artifact_html>\n"
    )


def test_a_tagged_reply_becomes_a_single_file_artifact() -> None:
    art = parse_tagged_artifact(_reply())
    assert (art.title, art.summary, art.entry) == ("Pomodoro Pro", "A focused timer.", "index.html")
    assert [(f.path, f.language, f.content) for f in art.files] == [("index.html", "html", HTML)]
    assert art.preview_html == HTML


def test_the_html_runs_to_the_last_closing_tag() -> None:
    """An app whose own source spells the closing tag cannot cut itself short."""
    html = '<!doctype html><script>const t = "</artifact_html>";</script>'
    art = parse_tagged_artifact(_reply(html=html))
    assert art.preview_html == html


def test_title_and_summary_are_read_only_from_before_the_html() -> None:
    """The app's own text cannot supply (or override) the artifact's name."""
    html = "<!doctype html><p><artifact_title>Injected</artifact_title></p>"
    art = parse_tagged_artifact("<artifact_summary>s</artifact_summary>\n<artifact_html>" + html + "</artifact_html>")
    assert art.title == "Untitled app"
    assert art.preview_html == html


def test_over_long_prose_is_trimmed_not_failed() -> None:
    art = parse_tagged_artifact(_reply(title="T" * 200, summary="S" * 900))
    assert (len(art.title), len(art.summary)) == (80, 280)


def test_an_oversized_app_is_clamped_like_every_other_artifact() -> None:
    html = "<!doctype html><html><body>" + ("<p>x</p>\n" * 20_000) + "</body></html>"
    art = parse_tagged_artifact(_reply(html=html))
    assert len(art.preview_html) <= MAX_ARTIFACT_CHARS + 256
    assert "artifact truncated at" in art.preview_html


def test_a_reply_in_the_json_shape_is_still_accepted() -> None:
    payload = {
        "title": "Calc",
        "summary": "s",
        "files": [{"path": "index.html", "language": "html", "content": HTML}],
        "entry": "index.html",
        "preview_html": HTML,
    }
    assert parse_tagged_artifact(json.dumps(payload)).title == "Calc"


@pytest.mark.parametrize(
    "reply",
    [
        "I can't build that.",
        "<artifact_title>x</artifact_title><artifact_html>   </artifact_html>",
        "<artifact_html><!doctype html> cut off mid-stream",
    ],
    ids=["no-html", "empty-html", "unclosed-html"],
)
def test_a_reply_without_an_app_raises_value_error(reply: str) -> None:
    with pytest.raises(ValueError):
        parse_tagged_artifact(reply)


def test_both_prompts_share_one_brief_and_differ_in_upstream_section_length_target_and_output_shape() -> None:
    # The Claude path reads earlier steps from one fenced UPSTREAM_OUTPUTS
    # block, so its upstream section differs too; the rest of the brief is one.
    brief = INSTRUCTIONS[: INSTRUCTIONS.index("# Using the upstream context")]
    assert CLAUDE_INSTRUCTIONS.startswith(brief + CLAUDE_UPSTREAM + "# Length target")
    assert "<artifact_html>" in CLAUDE_INSTRUCTIONS and "<artifact_html>" not in INSTRUCTIONS
    assert "preview_html" in INSTRUCTIONS


# ── deferred features ───────────────────────────────────────────────────────


def _with_deferred(deferred: str, summary: str = "A barbershop booking app.") -> str:
    return (
        f"<artifact_title>Brass & Blade</artifact_title>\n<artifact_summary>{summary}</artifact_summary>\n"
        f"<artifact_deferred>{deferred}</artifact_deferred>\n<artifact_html>\n{HTML}\n</artifact_html>"
    )


def test_deferred_features_are_named_in_the_summary() -> None:
    art = parse_tagged_artifact(_with_deferred("online payments, SMS reminders"))
    assert art.summary == "A barbershop booking app. Deferred: online payments, SMS reminders."


def test_the_deferred_list_survives_a_long_summary() -> None:
    """The summary is bounded at 280; the main text gives way, never the list."""
    long = " ".join(f"Sentence {i} about the app." for i in range(30))
    art = parse_tagged_artifact(_with_deferred("online payments, SMS reminders", summary=long))
    assert len(art.summary) <= 280
    assert art.summary.endswith(" Deferred: online payments, SMS reminders.")
    assert art.summary.startswith("Sentence 0 about the app.")


@pytest.mark.parametrize("deferred", ["", "   ", "none", "None.", "n/a"])
def test_an_empty_deferred_list_adds_nothing(deferred: str) -> None:
    art = parse_tagged_artifact(_with_deferred(deferred))
    assert art.summary == "A barbershop booking app."


def test_a_reply_with_no_deferred_tag_keeps_its_summary() -> None:
    assert parse_tagged_artifact(_reply()).summary == "A focused timer."


def test_the_deferred_tag_is_read_only_from_before_the_html() -> None:
    html = "<!doctype html><p><artifact_deferred>everything</artifact_deferred></p>"
    art = parse_tagged_artifact(_reply(html=html))
    assert "Deferred" not in art.summary


def test_the_tagged_shape_asks_for_the_deferred_list() -> None:
    assert "<artifact_deferred>" in TAGGED_SHAPE
    assert "did not build" in " ".join(TAGGED_SHAPE.split())
