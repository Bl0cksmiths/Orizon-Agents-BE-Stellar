"""code.critic carries the draft's "Deferred: …" list into its final summary.

code.gen ends its summary with the features it did not build. The critic
rewrites the app and writes a summary of its own, so without this the buyer's
final artifact would silently drop what was left out. The list is kept whole
inside the 280-character summary, the way code.gen keeps it, and merged
without duplicates with any deferred list the critic's own reply names.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.registry import WORKERS
from app.agents.workers.code_gen import carry_deferred, split_deferred
from app.config import settings
from app.llm.testing import FakeClaude

APP = (
    '<!doctype html>\n<html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>t</title></head><body><main>ok</main><script>1</script></body></html>"
)


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def _draft(summary: str) -> dict[str, Any]:
    art = {
        "title": "Brass & Blade",
        "summary": summary,
        "files": [{"path": "index.html", "language": "html", "content": APP}],
        "entry": "index.html",
        "preview_html": APP,
    }
    return {"code.gen": {"artifact": art, "summary": "s"}}


def _reply(summary: str = "Polished booking app with keyboard support.", deferred: str | None = None) -> str:
    tag = f"<artifact_deferred>{deferred}</artifact_deferred>" if deferred is not None else ""
    return (
        f"<artifact_title>Brass & Blade</artifact_title><artifact_summary>{summary}</artifact_summary>"
        f"{tag}<artifact_html>{APP}</artifact_html>"
    )


def _polish(draft_summary: str) -> str:
    out = asyncio.run(WORKERS["agt_12r0"].run("a barbershop booking system", "r", context=_draft(draft_summary)))
    assert out["critic_notes"][0].startswith("polished:"), out["critic_notes"]
    return out["artifact"]["summary"]


# ── the helpers ─────────────────────────────────────────────────────────────


def test_split_deferred_reads_the_list_off_a_summary() -> None:
    assert split_deferred("Booking app. Deferred: online payments, SMS reminders.") == (
        "Booking app.",
        ["online payments", "SMS reminders"],
    )
    assert split_deferred("Booking app.") == ("Booking app.", [])


def test_carry_deferred_merges_without_duplicates_in_draft_order() -> None:
    merged = carry_deferred(
        "Polished app. Deferred: sms reminders, staff payroll.",
        "Booking app. Deferred: online payments, SMS reminders.",
    )
    assert merged == "Polished app. Deferred: online payments, SMS reminders, staff payroll."


def test_carry_deferred_leaves_a_summary_alone_when_nothing_was_deferred() -> None:
    assert carry_deferred("Polished app.", "Booking app.") == "Polished app."


# ── through the worker ──────────────────────────────────────────────────────


def test_the_drafts_deferred_list_reaches_the_final_summary(claude: FakeClaude) -> None:
    claude.reply(_reply())
    summary = _polish("Booking app. Deferred: online payments, SMS reminders.")
    assert summary == "Polished booking app with keyboard support. Deferred: online payments, SMS reminders."


def test_the_critics_own_deferred_list_is_merged_in(claude: FakeClaude) -> None:
    claude.reply(_reply(deferred="SMS reminders, staff payroll"))
    summary = _polish("Booking app. Deferred: online payments, SMS reminders.")
    assert summary.endswith(" Deferred: online payments, SMS reminders, staff payroll.")


def test_the_list_stays_whole_after_a_long_critic_summary(claude: FakeClaude) -> None:
    long = " ".join(f"Sentence {i} about the polish." for i in range(30))
    claude.reply(_reply(summary=long))
    summary = _polish("Booking app. Deferred: online payments, SMS reminders.")
    assert len(summary) <= 280
    assert summary.endswith(" Deferred: online payments, SMS reminders.")


def test_no_deferred_list_means_the_critics_summary_as_written(claude: FakeClaude) -> None:
    claude.reply(_reply())
    assert _polish("Booking app.") == "Polished booking app with keyboard support."
