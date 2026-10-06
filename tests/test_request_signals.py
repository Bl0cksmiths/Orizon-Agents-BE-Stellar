"""The planner's conservative read of whether a translation has a target language."""

from __future__ import annotations

import pytest

from app.services.request_signals import clearly_english, has_translation_target, names_a_language
from evals.orchestrator.dataset import load


@pytest.mark.parametrize(
    "text",
    [
        "Build a landing page for my bakery with opening hours",
        "Write a polite email reminding a client their invoice is 7 days overdue.",
        "Make me a calculator app and translate it",
    ],
)
def test_a_clearly_english_request_naming_no_language_has_no_target(text: str) -> None:
    assert clearly_english(text)
    assert not has_translation_target(text)


@pytest.mark.parametrize(
    "text",
    [
        "Translate our menu into Japanese",  # names one
        "Build a bilingual site for our clinic",  # asks for several
        "Write launch copy in English and Tagalog",
        "Pa-help naman gumawa ng 3 tagline para sa online ukay-ukay shop ko.",  # Taglish, ASCII
        "Escribe una descripcion corta de producto para una taza de cafe",  # Spanish without accents
        "Buatkan slogan singkat untuk warung kopi saya di Bandung.",  # Indonesian
        "カフェのメニューに載せるチーズケーキの短い紹介文を書いてください。",  # non-Latin script
        "Rédige un message de bienvenue",  # accented Latin
        "x",  # too little to call English
    ],
)
def test_anything_uncertain_keeps_the_translation(text: str) -> None:
    assert has_translation_target(text)


def test_every_non_english_request_in_the_eval_set_keeps_its_translation() -> None:
    cases = [c for c in load() if c.expected_verdict == "allow" and c.language != "en"]

    assert len(cases) >= 20
    assert [c.id for c in cases if not has_translation_target(c.intent)] == []


def test_naming_a_language_is_word_bounded() -> None:
    assert names_a_language("in Thai please")
    assert not names_a_language("a thaimassage booking page")
