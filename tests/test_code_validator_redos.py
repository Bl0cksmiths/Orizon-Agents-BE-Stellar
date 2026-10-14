"""The validator's tag scans: the same matches, in linear time.

Each rewritten pattern is checked against the pattern it replaced, kept here
verbatim as the oracle, then fed the input that made the old one backtrack.
"""

from __future__ import annotations

import re
import time

import pytest

from app.agents.workers.code_validator import _JS_URL, _external_stylesheets, scan_script_hazards, validate_html

_LEGACY_STYLESHEET = re.compile(
    r'<link\b[^>]*\srel=["\']stylesheet["\'][^>]*\shref=["\'](?!data:|#|/|\./|\.\./)([^"\']+)',
    re.IGNORECASE,
)
_LEGACY_JS_URL = re.compile(r"""(?:href|src|action)\s*=\s*["']?\s*javascript:([^"'>]*)""", re.IGNORECASE)

_JS_URL_CASES = [
    '<a href="javascript:eval(window.name)">x</a>',
    "<a href='javascript:alert(1)'>x</a>",
    "<a href=javascript:alert(1)>x</a>",
    '<a HREF = " javascript:go()">x</a>',
    "<form action=\n'\n  JavaScript:send()'>",
    '<img src="  javascript:x">',
    '<a href="https://example.com">x</a>',
    "<a href=  'javascript'>x</a>",
    'href="" javascript:no',
    'src="\'javascript:twice"',
    "href=" + " " * 40 + "x",
]


@pytest.mark.parametrize("html", _JS_URL_CASES)
def test_javascript_url_matches_exactly_as_before(html: str) -> None:
    def spans(pattern: re.Pattern[str]) -> list[tuple[tuple[int, int], str]]:
        return [(m.span(), m.group(1)) for m in pattern.finditer(html)]

    assert spans(_JS_URL) == spans(_LEGACY_JS_URL)


def test_an_unterminated_javascript_url_scan_finishes_fast() -> None:
    html = "<a href=" + " " * 200_000 + "x>"
    started = time.perf_counter()
    scan_script_hazards(html)
    assert time.perf_counter() - started < 2.0


_STYLESHEET_CASES = [
    '<link rel="stylesheet" href="https://cdn.example/a.css">',
    "<LINK REL='stylesheet' HREF='//cdn.example/a.css'>",
    '<link href="https://cdn.example/a.css" rel="stylesheet">',
    '<link rel="stylesheet" href="/local.css"><link rel="stylesheet" href="data:text/css,x">',
    '<link rel="stylesheet" href="a.css" href="b.css" href="./c.css">',
    '<link rel="icon" href="a.png"><link rel="stylesheet" href="https://x/y>z.css">',
    '<link href="a.css" <link rel="stylesheet" href="b.css">',
    '<link rel="preload" href="a.css"> rel="stylesheet" href="b.css"',
    '<link rel="stylesheet"\nhref="u.css"><link\trel="stylesheet" href="v.css">',
    '<link rel="stylesheet" href="#frag">',
    "<link" + ' rel="stylesheet"' * 40,
]


@pytest.mark.parametrize("html", _STYLESHEET_CASES)
def test_external_stylesheets_match_exactly_as_before(html: str) -> None:
    assert list(_external_stylesheets(html)) == [m.group(1) for m in _LEGACY_STYLESHEET.finditer(html)]


def test_a_link_of_repeated_rels_validates_fast() -> None:
    html = "<link" + ' rel="stylesheet"' * 50_000
    started = time.perf_counter()
    validate_html(html)
    assert time.perf_counter() - started < 2.0
