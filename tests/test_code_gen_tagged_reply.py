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
    INSTRUCTIONS,
    MAX_ARTIFACT_CHARS,
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


def test_both_prompts_share_one_brief_and_differ_only_in_the_output_shape() -> None:
    brief = INSTRUCTIONS[: INSTRUCTIONS.index("# OUTPUT SHAPE")]
    assert CLAUDE_INSTRUCTIONS.startswith(brief)
    assert "<artifact_html>" in CLAUDE_INSTRUCTIONS and "<artifact_html>" not in INSTRUCTIONS
    assert "preview_html" in INSTRUCTIONS
