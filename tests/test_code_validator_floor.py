"""The validator's depth floor catches stubs, not dense but complete apps.

The floor used to be "under 200 lines (counting blank ones) — feature-
incomplete". The live re-measure of 2026-10-06 (evals/orchestrator/reports/
2026-10-06-recheck/r3-code-length/) flagged two complete apps with it: code.gen's
barbershop booking system at 180 lines / 14.4 KB and code.critic's polish at
191 lines / 16.9 KB, both dense but working (`tests/fixtures/
code_gen_dense_complete_app.html` is the first, verbatim).

So the floor is now met by EITHER at least 200 non-blank lines OR at least
12 000 characters of source. 200 readable lines run about 8 KB, so 12 KB is half
again what the line rule already accepted, and still 17% under the smallest
complete app measured. A stub — a few hundred lines of padding or a handful of
real ones — meets neither.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.workers.code_validator import MIN_DEPTH_LINES, MIN_DEPTH_SOURCE_CHARS, validate_html

FIXTURE = Path(__file__).parent / "fixtures" / "code_gen_dense_complete_app.html"
KITS = sorted((Path(__file__).resolve().parent.parent / "app" / "demo_kits" / "artifacts").glob("*.html"))

STUB = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Barbershop booking</title>
  <style>
    html, body { height: 100%; margin: 0 }
    body { display: flex; font-family: system-ui, sans-serif }
  </style>
</head>
<body>
  <main>
    <h1>Book a cut</h1>
    <p>TODO: services, staff schedules, time slots, admin view.</p>
    <button id="book">Book</button>
  </main>
  <script>
    document.getElementById("book").addEventListener("click", () => {
      alert("Booking coming soon");
    });
  </script>
</body>
</html>
"""


def _depth(html: str) -> list[str]:
    return [v for v in validate_html(html) if "feature-incomplete" in v]


def test_a_real_stub_is_flagged() -> None:
    assert _depth(STUB) == [
        f"under 200 lines (24) and under 12 KB ({len(STUB) / 1000:.1f} KB) — feature-incomplete, add depth"
    ]


def test_a_stub_padded_with_blank_lines_is_still_flagged() -> None:
    assert _depth(STUB.replace("</main>", "</main>" + "\n" * 400))


def test_the_dense_complete_app_from_the_live_run_is_not_flagged() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    assert html.count("\n") < 200  # the case the old floor got wrong
    assert _depth(html) == []


def test_two_hundred_readable_lines_meet_the_floor_however_short() -> None:
    body = "\n".join(f"    <p>line {i}</p>" for i in range(MIN_DEPTH_LINES))
    html = STUB.replace("<main>", "<main>\n" + body)
    assert len(html) < MIN_DEPTH_SOURCE_CHARS
    assert _depth(html) == []


def test_twelve_kilobytes_of_source_meet_the_floor_on_fewer_lines() -> None:
    html = STUB.replace("<p>TODO", "<p>" + "x" * MIN_DEPTH_SOURCE_CHARS + " TODO")
    assert _depth(html) == []


def test_just_under_both_bounds_is_flagged() -> None:
    lines = "\n".join(f"<p>{i}</p>" for i in range(MIN_DEPTH_LINES - 30))
    html = STUB.replace("<main>", "<main>\n" + lines)
    assert sum(1 for line in html.splitlines() if line.strip()) < MIN_DEPTH_LINES
    assert len(html) < MIN_DEPTH_SOURCE_CHARS
    assert _depth(html)


@pytest.mark.parametrize("path", KITS, ids=lambda p: p.name)
def test_the_curated_artifacts_meet_the_floor(path: Path) -> None:
    assert _depth(path.read_text(encoding="utf-8")) == []
