"""`.env.example` and `render.yaml` declare every model-layer setting, at the code's defaults.

The Render dashboard overrides render.yaml, so these files are documentation —
which is exactly why they drift unnoticed. The two API keys must be dashboard
secrets (`sync: false`), never values in git.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY")
DOCUMENTED = (
    "ORCHESTRATOR_PROVIDER",
    "CLAUDE_MODEL_LOW",
    "CLAUDE_MODEL_MODERATE",
    "CLAUDE_MODEL_COMPLEX",
    "CLAUDE_TIMEOUT_SECONDS",
    "CLAUDE_MAX_RETRIES",
    "CLAUDE_SERVER_FALLBACKS",
    "TYPESAFE_MODEL",
    "JEV_TIMEOUT_SECONDS",
    "JEV_MAX_RETRIES",
    "LLM_DAILY_SPEND_CAP_USD",
)


def _env_example() -> dict[str, str]:
    text = (ROOT / ".env.example").read_text()
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, flags=re.MULTILINE))


def _render_entries() -> dict[str, str]:
    """key -> the line after it (`value: ...` or `sync: false`)."""
    lines = (ROOT / "render.yaml").read_text().splitlines()
    entries: dict[str, str] = {}
    for i, line in enumerate(lines):
        found = re.match(r"\s*- key: (\S+)\s*$", line)
        if found:
            entries[found.group(1)] = lines[i + 1].strip()
    return entries


def _as_setting(name: str, raw: str) -> object:
    """The value as Settings would parse it from the environment."""
    return Settings(_env_file=None, **{name.lower(): raw}).__getattribute__(name.lower())  # type: ignore[call-arg]


def _default(name: str) -> object:
    return Settings.model_fields[name.lower()].default


def test_the_api_keys_are_dashboard_secrets_and_empty_in_the_example() -> None:
    render, example = _render_entries(), _env_example()
    for key in SECRETS:
        assert render[key] == "sync: false", key
        assert example[key] == "", key


@pytest.mark.parametrize("key", DOCUMENTED)
def test_each_setting_is_documented_at_its_default(key: str) -> None:
    example, render = _env_example(), _render_entries()
    rendered = re.fullmatch(r'value: "?([^"]*)"?', render[key])
    assert rendered is not None, f"render.yaml {key}: {render[key]}"
    expected = _default(key)
    for source, raw in ((".env.example", example[key]), ("render.yaml", rendered.group(1))):
        value = _as_setting(key, raw)
        if key == "ORCHESTRATOR_PROVIDER":  # "auto" and the default "" are the same choice
            assert value in ("auto", expected), (source, value)
        else:
            assert value == expected, (source, key, value, expected)
