"""The redaction every line of the evidence sheet passes through.

The evidence tool holds no secret. Its inputs are the lifecycle harness's
evidence files, which the harness already writes through its own redactor —
but a row's `detail` quotes server messages, and a file edited by hand can
carry anything. So every line printed, and every line written to the sheet,
the description and the JSON, is scrubbed of anything SHAPED like a secret: a
Stellar seed and a long base64 run (an XDR envelope, a signature). Public
keys, contract ids and transaction hashes are left alone on purpose: they are
the evidence. The rule is the harness's (`scripts/lifecycle/redact.py`),
re-stated so the tools stay independent.
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
    """Where every line the evidence tool prints goes. Never `print` around it."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def say(self, line: str = "") -> None:
        stream = self._stream if self._stream is not None else sys.stdout
        stream.write(scrub(line) + "\n")
        stream.flush()
