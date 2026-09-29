"""The pre-flight's one output path, and the redaction every line passes through.

The pre-flight holds no secret and needs none: every flag is a public key, a
URL or a number. But an operator in a hurry pastes the wrong thing — a buyer's
`S...` seed where its `G...` address belongs — and a server's error message
can echo whatever it was sent. So every line printed, and every line written to
the Markdown and JSON reports, passes through `scrub`, which masks anything
SHAPED like a secret: a Stellar seed and a long base64 run (an XDR envelope, a
signature). Public keys, contract ids and transaction hashes are left alone on
purpose; they are what the report is for.

The rule is the lifecycle harness's (`scripts/lifecycle/redact.py`), re-stated
so the tools stay independent.
"""

from __future__ import annotations

import re
import sys
from typing import TextIO

_SEED = re.compile(r"\bS[A-Z2-7]{55}\b")
# Hex hashes cannot match: they contain no `+`, `/` or `=`, and a run is only
# masked when it carries at least one of them.
_B64_BLOB = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_B64_MARK = re.compile(r"[+/=]")
# URLs are left out of the base64 check (their `/`-separated paths would match
# it); a seed is still masked inside one.
_URL = re.compile(r"https?://\S+")

MASK = "[redacted]"
SEED_SHAPE = re.compile(r"^S[A-Z2-7]{55}$")


def _scrub_shapes(text: str) -> str:
    text = _SEED.sub(MASK, text)
    return _B64_BLOB.sub(lambda m: MASK if _B64_MARK.search(m.group(0)) else m.group(0), text)


def scrub(text: str) -> str:
    out: list[str] = []
    last = 0
    for url in _URL.finditer(text):
        out.append(_scrub_shapes(text[last : url.start()]))
        out.append(_SEED.sub(MASK, url.group(0)))
        last = url.end()
    out.append(_scrub_shapes(text[last:]))
    return "".join(out)


class Console:
    """Where every line the pre-flight prints goes. Never `print` around it."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def say(self, line: str = "") -> None:
        stream = self._stream if self._stream is not None else sys.stdout
        stream.write(scrub(line) + "\n")
        stream.flush()
