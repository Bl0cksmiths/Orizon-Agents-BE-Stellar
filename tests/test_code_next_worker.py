"""code.next on Claude, on FakeClaude only: a streamed, tagged set of Next.js
files, capped at Sonnet like code.gen, validated in code (paths, sizes,
secrets, network and execution calls), previewed as a static source page, and
never simulated off Claude."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents.registry import WORKERS
from app.agents.workers import claude_step, code_next
from app.agents.workers.claude_step import ModelStepError
from app.agents.workers.context import upstream
from app.config import settings
from app.llm.errors import LLMUnavailable
from app.llm.testing import FakeClaude

INTENT = "A Next.js pricing page component with a monthly/yearly toggle"
RATIONALE = "the buyer's site is a Next.js app"
WORKER = code_next.CodeNext()

PAGE = """import { Pricing } from "../components/Pricing";

export default function Page() {
  return <Pricing />;
}"""
COMPONENT = """'use client';
import { useState } from "react";
import styles from "./Pricing.module.css";

export function Pricing(): JSX.Element {
  const [yearly, setYearly] = useState(false);
  return (
    <section className={styles.wrap}>
      <button aria-pressed={yearly} onClick={() => setYearly(!yearly)}>Yearly</button>
    </section>
  );
}"""
CSS = ".wrap { display: grid; gap: 1rem; }"


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch, fake_claude: FakeClaude) -> FakeClaude:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    return fake_claude


def _tagged(
    *files: tuple[str, str], title: str = "Pricing", summary: str = "A pricing toggle.", deferred: str = ""
) -> str:
    files = files or (
        ("app/page.tsx", PAGE),
        ("components/Pricing.tsx", COMPONENT),
        ("components/Pricing.module.css", CSS),
    )
    body = "\n".join(f'<next_file path="{path}">\n{content}\n</next_file>' for path, content in files)
    return (
        f"<next_title>{title}</next_title>\n<next_summary>{summary}</next_summary>\n"
        f"<next_deferred>{deferred}</next_deferred>\n{body}"
    )


def run(context: dict[str, Any] | None = None, tier: Any = None, intent: str = INTENT) -> dict[str, Any]:
    return asyncio.run(WORKER.run(intent, RATIONALE, context=context, tier=tier))


def _failure(**kw: Any) -> ModelStepError:
    with pytest.raises(ModelStepError) as info:
        run(**kw)
    return info.value


def test_next_files_are_streamed_on_sonnet_and_returned_as_an_artifact(claude: FakeClaude) -> None:
    claude.reply(_tagged())
    out = run()

    (call,) = claude.calls
    assert (call.purpose, call.model, call.effort) == ("worker.code.next", "claude-sonnet-5-5", "low")
    assert call.stream is True and call.json_schema is None
    assert call.system == code_next.CLAUDE_INSTRUCTIONS and call.max_tokens == code_next.MAX_TOKENS
    assert "BEGIN USER_INPUT" in call.user and call.user.rstrip().endswith(
        "Return the Next.js files in the tagged shape."
    )

    art = out["artifact"]
    assert set(out) == {"summary", "artifact", "counts", "validator_violations"}
    assert out["summary"] == "Pricing — A pricing toggle."
    assert [(f["path"], f["language"]) for f in art["files"]] == [
        ("app/page.tsx", "tsx"),
        ("components/Pricing.tsx", "tsx"),
        ("components/Pricing.module.css", "css"),
    ]
    assert art["files"][1]["content"] == COMPONENT + "\n"
    assert art["entry"] == "app/page.tsx" and art["framework"] == "next"
    # The preview is a static, hardened overview of the source: no script runs.
    preview = art["preview_html"]
    assert "Content-Security-Policy" in preview and "<script" not in preview
    assert "useState(false)" in preview and "&lt;Pricing /&gt;" in preview
    assert out["validator_violations"] == []
    assert out["counts"]["files"] == 3
    # What a later step (deploy.v0, code.critic) receives through the handoff.
    assert "Files: 3 (entry app/page.tsx)" in upstream({"code.next": out}, "code.critic").text()


@pytest.mark.parametrize(("tier", "model"), [("low", "claude-haiku-4-5"), ("complex", "claude-sonnet-5-5")])
def test_the_tier_is_capped_at_sonnet(claude: FakeClaude, tier: str, model: str) -> None:
    claude.reply(_tagged())
    run(tier=tier)
    assert claude.calls[0].model == model
    assert WORKER.max_tier == "moderate"


def test_the_upstream_design_and_copy_are_fenced_into_the_prompt(claude: FakeClaude) -> None:
    design = {"palette": {"bg": "#0B0414", "primary": "#7C5CFF"}, "typography": {"family_ui": "Inter, sans-serif"}}
    copy = {"hero": {"headline": "UPSTREAM-HEADLINE Simple pricing"}, "sections": []}
    claude.reply(_tagged())
    run(context={"design.figma": design, "copywrite.v3": copy})
    user = claude.calls[0].user
    assert user.index("END USER_INPUT") < user.index("BEGIN UPSTREAM_OUTPUTS") < user.index("--primary: #7C5CFF")
    assert user.index("UPSTREAM-HEADLINE") < user.index("END UPSTREAM_OUTPUTS")
    assert WORKER.upstream_sources({"design.figma": design, "copywrite.v3": copy}) == ["design.figma", "copywrite.v3"]


def test_the_facts_rule_is_part_of_the_brief() -> None:
    """The live smoke of 2026-10-06 showed made-up plan prices on a pricing page."""
    assert "never invent prices" in code_next.CLAUDE_INSTRUCTIONS
    assert "[placeholder: monthly price]" in code_next.CLAUDE_INSTRUCTIONS


def test_the_deferred_list_reaches_the_summary(claude: FakeClaude) -> None:
    claude.reply(_tagged(deferred="currency switcher, coupon codes"))
    assert run()["artifact"]["summary"] == "A pricing toggle. Deferred: currency switcher, coupon codes."


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        ("/etc/passwd.ts", "path_shape"),
        ("../outside.ts", "path_segment"),
        ("app/../../x.ts", "path_segment"),
        (".env.local", "path_segment"),
        ("app/.secret/x.ts", "path_segment"),
        ("app//page.tsx", "path_shape"),
        ("app/page.tsx;rm -rf", "path_shape"),
        ("scripts/install.sh", "file_type"),
        ("app/" + "a" * 130 + ".tsx", "path_length"),
    ],
)
def test_a_file_with_an_unsafe_path_is_dropped_and_reported(claude: FakeClaude, path: str, problem: str) -> None:
    claude.reply(_tagged(("app/page.tsx", PAGE), (path, "export {};")))
    out = run()
    assert [f["path"] for f in out["artifact"]["files"]] == ["app/page.tsx"]
    assert out["validator_violations"] == [f"dropped_file:{problem}"]


def test_next_route_segments_are_valid_paths() -> None:
    for path in ("app/blog/[slug]/page.tsx", "app/(marketing)/page.tsx", "app/@modal/default.tsx", "lib/format.ts"):
        assert code_next.path_problem(path) is None


def test_a_secret_in_the_code_is_redacted_and_reported(claude: FakeClaude) -> None:
    leaky = 'const KEY = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX";\nexport default function Page() { return null; }'
    claude.reply(_tagged(("app/page.tsx", leaky)))
    out = run()
    content = out["artifact"]["files"][0]["content"]
    assert "ABCDEFGHIJKLMNOPQRSTUVWX" not in content and "[redacted secret]" in content
    assert "ABCDEFGHIJKLMNOPQRSTUVWX" not in out["artifact"]["preview_html"]
    assert out["validator_violations"] == ["secret_redacted:app/page.tsx"]


def test_a_network_call_the_request_did_not_ask_for_is_reported(claude: FakeClaude) -> None:
    calling = 'export default async function Page() { await fetch("https://evil.example/x"); return null; }'
    claude.reply(_tagged(("app/page.tsx", calling)))
    assert run()["validator_violations"] == ["network_call:app/page.tsx"]


def test_a_network_call_the_request_asked_for_is_not_a_violation(claude: FakeClaude) -> None:
    calling = "export default async function Page() { await fetch(process.env.API_URL!); return null; }"
    claude.reply(_tagged(("app/page.tsx", calling)))
    assert run(intent="A Next.js page that lists products from our REST API")["validator_violations"] == []


def test_code_execution_is_always_reported(claude: FakeClaude) -> None:
    unsafe = "export default function Page() { return <div dangerouslySetInnerHTML={{ __html: '' }} />; }"
    claude.reply(_tagged(("app/page.tsx", unsafe)))
    assert run(intent="A page that fetches from our API")["validator_violations"] == ["unsafe_call:app/page.tsx"]


def test_files_and_sizes_are_bounded(claude: FakeClaude, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_next, "MAX_FILE_CHARS", 200)
    many = [(f"components/C{i}.tsx", "export {};") for i in range(10)]
    claude.reply(_tagged(("app/page.tsx", "x" * 300), *many))
    out = run()
    assert len(out["artifact"]["files"]) == code_next.MAX_FILES
    assert out["validator_violations"][0] == "dropped_file:too_large:app/page.tsx"
    assert out["validator_violations"][1:] == [
        "dropped_file:too_many:components/C8.tsx",
        "dropped_file:too_many:components/C9.tsx",
    ]


@pytest.mark.parametrize(
    ("script", "rule"),
    [
        (lambda c: c.reply("Here is a description of the component instead."), "invalid_output"),
        (
            lambda c: c.reply(
                _tagged(
                    ("scripts/x.sh", "echo"),
                )
            ),
            "invalid_output",
        ),
        (lambda c: c.reply(_tagged(("app/page.tsx", PAGE), ("app/page.tsx", PAGE))), "invalid_output"),
        (lambda c: c.refuse(category="cyber", explanation="MODEL-PROSE"), "model_refused"),
        (lambda c: c.truncate(partial='<next_title>P</next_title><next_file path="app/page.tsx">'), "model_truncated"),
        (lambda c: c.fail(LLMUnavailable("overloaded")), "model_unavailable"),
    ],
    ids=["no-files", "no-usable-file", "duplicate-path", "refused", "truncated", "unavailable"],
)
def test_files_the_model_did_not_deliver_fail_the_step(claude: FakeClaude, script: Any, rule: str) -> None:
    script(claude)
    err = _failure()
    assert err.rule == rule and "MODEL-PROSE" not in str(err)


def test_a_slow_stream_fails_inside_the_step_budget(claude: FakeClaude, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_step, "STREAM_BUDGET_SECONDS", 0.05)
    claude.cut_stream("<next_title>P</next_title>", purpose="worker.code.next")
    assert _failure().rule == "model_truncated"


def test_off_claude_the_step_is_not_attempted(fake_claude: FakeClaude) -> None:
    assert _failure().rule == "model_not_configured"
    assert fake_claude.calls == []
    assert WORKER.step_model(None, None) is None


def test_the_registry_serves_the_real_worker() -> None:
    assert isinstance(WORKERS["agt_03d9"], code_next.CodeNext)
