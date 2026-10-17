"""code.critic's run: which draft it reviews, and how it reports a polish.

Pins the paths the model-driven suites do not reach: a draft whose preview is
empty is read from its files, a revision with more structural violations is
reverted, and a kit's checklist is passed on as required items and reported.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.agents.workers.code_critic_worker import CodeCriticWorker
from app.agents.workers.code_validator import validate_html

APP = (
    '<!doctype html>\n<html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
    "<title>t</title></head><body><main>ok</main><script>1</script></body></html>"
)
BROKEN = "<p>no document</p>"


class _StubCritic:
    """Records what the worker asked for and answers with a fixed revision."""

    def __init__(self, revised: dict[str, Any]) -> None:
        self.revised = revised
        self.calls: list[dict[str, Any]] = []

    async def refine(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.revised


def _worker(revised: dict[str, Any]) -> tuple[CodeCriticWorker, _StubCritic]:
    worker = CodeCriticWorker()
    stub = _StubCritic(revised)
    worker._critic = stub  # type: ignore[assignment]
    return worker, stub


def _artifact(preview: str, files: list[dict[str, str]], entry: str = "index.html") -> dict[str, Any]:
    return {"title": "Draft", "summary": "s", "files": files, "entry": entry, "preview_html": preview}


def _run(worker: CodeCriticWorker, context: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(worker.run("a booking app", "r", context=context))


def _revision(html: str = APP) -> dict[str, Any]:
    return {"title": "Polished", "summary": "p", "files": [], "entry": "index.html", "preview_html": html}


def test_an_empty_preview_is_read_from_the_entry_file() -> None:
    worker, stub = _worker(_revision())
    files = [{"path": "other.html", "content": "<p>other</p>"}, {"path": "index.html", "content": APP}]
    _run(worker, {"code.gen": {"artifact": _artifact("  ", files)}})
    assert stub.calls[0]["draft_html"] == APP


def test_an_empty_preview_with_no_entry_match_is_read_from_the_first_file() -> None:
    worker, stub = _worker(_revision())
    files = [{"path": "a.html", "content": APP}, {"path": "b.html", "content": BROKEN}]
    _run(worker, {"code.gen": {"artifact": _artifact("", files, entry="missing.html")}})
    assert stub.calls[0]["draft_html"] == APP


def test_an_empty_preview_with_no_files_reviews_an_empty_draft() -> None:
    worker, stub = _worker(_revision())
    _run(worker, {"code.gen": {"artifact": _artifact("", [])}})
    assert stub.calls[0]["draft_html"] == ""


def test_a_revision_with_more_violations_is_reverted_to_the_draft() -> None:
    assert len(validate_html(BROKEN)) > len(validate_html(APP))
    worker, _ = _worker(_revision(BROKEN))
    draft = _artifact(APP, [{"path": "index.html", "content": APP}])
    out = _run(worker, {"code.gen": {"artifact": draft}})
    assert out["critic_notes"] == [
        f"reverted: revised had {len(validate_html(BROKEN))} structural issues vs. {len(validate_html(APP))} in draft"
    ]
    assert out["artifact"]["title"] == "Draft"
    assert out["summary"] == "Draft · " + out["critic_notes"][0]
    assert out["counts"] == {"violations": len(validate_html(APP)), "kit_requirements": 0, "notes": 1}


def test_a_kits_checklist_is_required_of_the_critic_and_reported() -> None:
    worker, stub = _worker(_revision())
    draft = _artifact(APP, [{"path": "index.html", "content": APP}])
    kit = {"kit_id": "tetris", "critic_checklist": ["keyboard controls", "a score"]}
    out = _run(worker, {"code.gen": {"artifact": draft}, "kit": kit})
    assert stub.calls[0]["violations"][:2] == ["REQUIRED (tetris): keyboard controls", "REQUIRED (tetris): a score"]
    assert out["critic_notes"][1] == "applied 2 kit requirements"
    assert out["critic_notes"][0].startswith("polished: ")
    assert out["counts"]["kit_requirements"] == 2


def test_a_single_kit_requirement_is_reported_in_the_singular() -> None:
    worker, _ = _worker(_revision())
    draft = _artifact(APP, [{"path": "index.html", "content": APP}])
    out = _run(worker, {"code.gen": {"artifact": draft}, "kit": {"critic_checklist": ["one"]}})
    assert out["critic_notes"][1] == "applied 1 kit requirement"
