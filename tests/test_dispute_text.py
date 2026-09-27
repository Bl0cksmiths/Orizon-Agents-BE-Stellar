"""The cleaner for a dispute's free text — the buyer's reason and the adjudicator's note.

D-059 and D-062. Both fields are read by a person and never by a model, so
`dispute_svc.clean_dispute_text` cleans them for a reader: controls and bidi
overrides out, text with nothing visible treated as empty, and nothing else
changed — no marker redaction and no truncation. `tests/test_dispute_svc.py`
holds the reason's path through `open_dispute`; this file holds the rules
themselves and the note's path through `reject`.

Hermetic: the in-memory dispute store, nothing signed.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services import dispute_store, dispute_svc
from app.services.dispute_store import DisputeRecord
from app.services.dispute_svc import DisputeError, clean_dispute_text

INVISIBLE = ["​", "‌⁠", "﻿", "­", "‮", "ㅤ", "⠀", "ᅟᅠ", "͏"]


@pytest.fixture(autouse=True)
def _fresh_store():
    dispute_store._store = None
    yield
    dispute_store._store = None


def _open_dispute() -> DisputeRecord:
    record = DisputeRecord(
        id=dispute_store.new_dispute_id(),
        job_id_hex="9f8e7d6c5b4a39281706f5e4d3c2b1a0",
        task_id="tsk_text",
        step_index=0,
        agent_id="agt_writer",
        payer="G" + "A" * 55,
        reason="the draft ignored the brief",
        status="open",
        charged_usdc=0.05,
        creditable_usdc=0.05,
        opened_at=time.time(),
    )
    return asyncio.run(dispute_store.get_dispute_store().open_dispute(record))


# ── the rules ───────────────────────────────────────────────────


@pytest.mark.parametrize("text", INVISIBLE + ["\x85", "\x9b\x00", " \t\n ", ""])
def test_text_with_nothing_visible_cleans_to_empty(text: str) -> None:
    assert clean_dispute_text(text) == ""


def test_none_cleans_to_empty_rather_than_raising() -> None:
    assert clean_dispute_text(None) == ""


def test_c0_and_c1_controls_become_spaces_and_tab_and_newline_survive() -> None:
    assert clean_dispute_text("a\x00b\x1bc\x7fd\x85e\x9bf\tg\nh") == "a b c d e f\tg\nh"


def test_bidi_controls_and_lone_surrogates_are_dropped() -> None:
    assert clean_dispute_text("a‮b⁦c⁩d‏e؜f\ud800g") == "abcdefg"


def test_ordinary_text_is_untouched() -> None:
    """No prompt-fence defence: marker-shaped words, `====` runs and the
    joiners emoji and Persian need all come through exactly."""
    for text in [
        "THE END RESULT WAS WRONG and BEGIN SECTION was missing",
        "==== ==== ====",
        "café \U0001f469‍\U0001f4bb می‌خوام",
    ]:
        assert clean_dispute_text(text) == text


def test_cleaning_never_lengthens_or_cuts() -> None:
    text = "x" * 5_000 + " END ABCD"
    assert clean_dispute_text(text) == text


# ── the adjudicator's note, through reject ──────────────────────


@pytest.mark.parametrize("note", INVISIBLE, ids=[f"U+{ord(n[0]):04X}" for n in INVISIBLE])
def test_a_rejection_note_nobody_can_see_is_refused(note: str) -> None:
    """D-059, the note's half: the buyer's receipt would show `rejected`
    beside an explanation that displays as nothing."""
    dispute = _open_dispute()

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.reject(dispute.id, note=note))

    assert (refused.value.code, refused.value.status_code) == ("rejection_reason_required", 422)


def test_a_rejection_note_is_stored_cleaned_and_otherwise_as_written() -> None:
    dispute = _open_dispute()

    rejected = asyncio.run(dispute_svc.reject(dispute.id, note="THE END RESULT \x9bmatched‮ the brief"))

    assert rejected.note == "THE END RESULT  matched the brief"


def test_a_rejection_note_past_the_limit_is_refused_and_never_cut() -> None:
    """D-062, the note's half. The edge bounds it at the same number, so only an
    in-process caller reaches this; it is refused rather than trimmed."""
    dispute = _open_dispute()

    with pytest.raises(DisputeError) as refused:
        asyncio.run(dispute_svc.reject(dispute.id, note="n" * (dispute_svc.MAX_REASON_CHARS + 1)))

    assert (refused.value.code, refused.value.status_code) == ("rejection_reason_too_long", 422)
    assert asyncio.run(dispute_store.get_dispute_store().get_dispute(dispute.id)).status == "open"
