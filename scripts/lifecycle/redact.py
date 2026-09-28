"""The harness's one output path, and the redaction every line passes through.

The run holds four kinds of secret: the buyer's seed, the operator key it
adjudicates with, the task read token and the dispute read grant (each a
credential for its task), and the signed authorize envelope (signature
material). None may reach the terminal, a log, or the evidence file.

That is enforced here, as a property of the output path rather than a promise
about each line: `Console.say` is the only way the harness prints, and it runs
every line through `Redactor.scrub`, which masks

  * every secret VALUE the run has registered, wherever it appears, and
  * anything SHAPED like a secret it was never told about — a Stellar seed
    (`S` + 55 base32 characters) and any long base64 run, which is what an XDR
    envelope or a signature looks like.

Public keys (`G...`) and transaction hashes (64 hex) are left alone on purpose:
they are the evidence, and a report that masked them would be useless.
"""

from __future__ import annotations

import re
import sys
from typing import TextIO

# A Stellar secret seed, as strkey spells one.
_SEED = re.compile(r"\bS[A-Z2-7]{55}\b")
# A base64 run long enough to be an envelope or a signature (an ed25519
# signature is 88 characters of base64). Hex hashes cannot match: they contain
# no `+`, `/` or `=`, and the class below requires at least one of them.
_B64_BLOB = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_B64_MARK = re.compile(r"[+/=]")
# A URL is left out of the shape check: its path is `/`-separated words that
# the base64 class would otherwise swallow, and the explorer links are the
# evidence. No URL the harness builds carries a secret — credentials travel in
# headers — and a registered secret is still masked inside one.
_URL = re.compile(r"https?://\S+")

MASK = "[redacted]"


def _scrub_shapes(text: str) -> str:
    text = _SEED.sub(MASK, text)
    return _B64_BLOB.sub(lambda m: MASK if _B64_MARK.search(m.group(0)) else m.group(0), text)


class Redactor:
    """Masks registered secret values and secret-shaped text."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, value: str | None) -> None:
        """Remember `value` as a secret. Short values are refused as secrets
        because masking every occurrence of a three-character string would
        shred the output without protecting anything."""
        if value and len(value) >= 8:
            self._secrets.add(value)

    def scrub(self, text: str) -> str:
        # Longest first, so a secret that contains another is masked whole.
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, MASK)
        out: list[str] = []
        last = 0
        for url in _URL.finditer(text):
            out.append(_scrub_shapes(text[last : url.start()]))
            out.append(_SEED.sub(MASK, url.group(0)))
            last = url.end()
        out.append(_scrub_shapes(text[last:]))
        return "".join(out)


class Console:
    """Where every line the harness prints goes. Never `print` around it."""

    def __init__(self, redactor: Redactor, stream: TextIO | None = None) -> None:
        self.redactor = redactor
        self._stream = stream

    def say(self, line: str = "") -> None:
        stream = self._stream if self._stream is not None else sys.stdout
        stream.write(self.redactor.scrub(line) + "\n")
        stream.flush()
