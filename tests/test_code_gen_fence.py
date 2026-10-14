"""code.gen's fence stripping: the same unwrapping, in linear time.

`coerce_artifact` peels a ```json fence off a string reply before parsing it.
The fence pattern used to be `^```(?:json)?\\s*\\n?(.*?)\\n?```$`, whose
overlapping whitespace quantifiers backtrack super-linearly on an unclosed
fence. The replacement must unwrap every reply exactly as the old one did.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest

from app.agents.workers.code_gen import CodeArtifact, coerce_artifact

_LEGACY_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)

_PAYLOAD: dict[str, Any] = {
    "title": "t",
    "summary": "s",
    "files": [{"path": "index.html", "language": "html", "content": "<html></html>"}],
    "entry": "index.html",
    "preview_html": "<html></html>",
}
_BODY = json.dumps(_PAYLOAD)


def _legacy_coerce(content: str) -> CodeArtifact:
    s = content.strip()
    fence = _LEGACY_FENCE.match(s)
    if fence:
        s = fence.group(1).strip()
    return CodeArtifact.model_validate_json(s)


@pytest.mark.parametrize(
    "reply",
    [
        f"```json\n{_BODY}\n```",
        f"```\n{_BODY}\n```",
        f"```json{_BODY}```",
        f"```{_BODY}```",
        f"  ```json\n\n  {_BODY}  \n\n```  ",
        f"```json\r\n{_BODY}\r\n```",
        f"```json \t\n{_BODY}\n\t```",
        _BODY,
    ],
)
def test_a_fenced_reply_unwraps_exactly_as_before(reply: str) -> None:
    assert coerce_artifact(reply) == _legacy_coerce(reply)


def test_an_unclosed_fence_fails_fast() -> None:
    reply = "```json" + "\n" * 50_000 + "x"
    started = time.perf_counter()
    with pytest.raises(ValueError, match="without JSON object"):
        coerce_artifact(reply)
    assert time.perf_counter() - started < 2.0
