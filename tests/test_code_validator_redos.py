"""The validator's tag scans: the same matches, in linear time.

Each rewritten pattern is checked against the pattern it replaced, kept here
verbatim as the oracle, then fed the input that made the old one backtrack.
"""

from __future__ import annotations

import re
import time

import pytest

from app.agents.workers.code_validator import _JS_URL, scan_script_hazards

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
