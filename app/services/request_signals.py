"""Pure, conservative reads of a buyer's request that planning acts on.

The run loop does not attempt a translate.42 step that has no target
language: the worker fails it as `no_target_language` (unbilled), and it gets
one from the request — a language the request names, or, when it names none,
the language the request is written in if that is not English. A plan that
holds such a step would still show the buyer its price to authorize, so
planning drops it (`orchestrator_svc._compose`) — but only when it is sure.

`has_translation_target` therefore answers False only for a request that is
clearly written in English AND names no language; anything uncertain (a
non-ASCII script, Taglish, Spanish without accents, a word like "bilingual")
answers True and the step stays. A wrong True costs at worst one unbilled
step; a wrong False would take away a translation the buyer asked for.

Pure: no I/O, no model call — it runs on every plan.
"""

from __future__ import annotations

import re

# Languages a buyer might name as a target, in English and in their own words
# (lower case). A name that is also an English word ("polish") only errs
# towards keeping the step.
LANGUAGE_NAMES: frozenset[str] = frozenset(
    {
        "english",
        "spanish",
        "espanol",
        "español",
        "tagalog",
        "filipino",
        "pilipino",
        "taglish",
        "cebuano",
        "bisaya",
        "visayan",
        "ilocano",
        "ilokano",
        "hiligaynon",
        "ilonggo",
        "kapampangan",
        "bikol",
        "waray",
        "japanese",
        "nihongo",
        "chinese",
        "mandarin",
        "cantonese",
        "korean",
        "thai",
        "vietnamese",
        "indonesian",
        "bahasa",
        "malay",
        "hindi",
        "bengali",
        "urdu",
        "punjabi",
        "tamil",
        "telugu",
        "marathi",
        "gujarati",
        "nepali",
        "sinhala",
        "khmer",
        "lao",
        "burmese",
        "arabic",
        "hebrew",
        "turkish",
        "persian",
        "farsi",
        "russian",
        "ukrainian",
        "polish",
        "czech",
        "romanian",
        "hungarian",
        "greek",
        "german",
        "deutsch",
        "french",
        "français",
        "francais",
        "italian",
        "portuguese",
        "português",
        "portugues",
        "dutch",
        "swedish",
        "norwegian",
        "danish",
        "finnish",
        "catalan",
        "swahili",
        "afrikaans",
        "zulu",
        "amharic",
    }
)

# Words that say several languages are wanted without naming them.
_MULTILINGUAL: frozenset[str] = frozenset(
    {"bilingual", "multilingual", "trilingual", "languages", "localize", "localise", "localization", "i18n"}
)

# English function words: their share is how "clearly English" is judged.
_ENGLISH: frozenset[str] = frozenset(
    """a an the and or but of to in on for with at by from as is are be was were it its this that these those
    my our your their his her me us you we i they them do does can could would should will please make
    build write create give into about than then so if not no all any some each every""".split()
)

# Function words of the other languages buyers on this console write in —
# Tagalog / Taglish, Cebuano, Spanish, Portuguese, Indonesian / Malay, French,
# German — so an ASCII request in one of them is never read as English.
_FOREIGN: frozenset[str] = frozenset(
    """ng mga sa ang na mo ko ako namin natin ninyo para naman po ba itong yung kung tapos gawa gawan gumawa
    pwede paki pakigawa hímoa himoa og nga among ug kay
    el la los las del y con mi tu su una por que es muy
    o os um uma meu minha com não nao
    dan yang untuk saya kami di ke dengan ini itu buatkan
    le les des et pour avec mon ma nos est une
    der die das und mit für fur ein eine ich wir""".split()
)

_WORD = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*", re.UNICODE)

# Below this share of English function words a request is not "clearly English".
_ENGLISH_SHARE = 0.15


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def names_a_language(text: str) -> bool:
    """Whether the request names a language, or asks for several."""
    words = set(_words(text))
    return bool(words & LANGUAGE_NAMES or words & _MULTILINGUAL)


def clearly_english(text: str) -> bool:
    """Whether the request is unmistakably written in English (conservative)."""
    if any(not ch.isascii() for ch in text if ch.isalpha()):
        return False
    words = _words(text)
    if not words:
        return False
    if any(w in _FOREIGN for w in words):
        return False
    return sum(w in _ENGLISH for w in words) / len(words) >= _ENGLISH_SHARE


def has_translation_target(text: str) -> bool:
    """False only when a translate.42 step would surely have no target language:
    the request is clearly English and names no language."""
    return names_a_language(text) or not clearly_english(text)
