"""The Claude/OpenAI switch, its settings, and what /readiness reports about it."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.llm import provider, spend
from app.llm.spend import Usage

VALID_C = "C" + "A" * 55
VALID_G = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"


@pytest.mark.parametrize(
    ("choice", "anthropic_key", "expected"),
    [
        ("", "", "openai"),
        ("", "sk-ant", "anthropic"),
        ("auto", "sk-ant", "anthropic"),
        ("AUTO", "", "openai"),
        ("openai", "sk-ant", "openai"),
        (" Anthropic ", "", "anthropic"),
    ],
)
def test_the_provider_is_claude_once_its_key_is_set_unless_named(
    monkeypatch: pytest.MonkeyPatch, choice: str, anthropic_key: str, expected: str
) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", choice)
    monkeypatch.setattr(settings, "anthropic_api_key", anthropic_key)
    assert provider.active_provider() == expected


def test_only_the_active_provider_key_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    monkeypatch.setattr(settings, "openai_api_key", "sk-openai")
    assert provider.provider_key_present() is False
    assert provider.provider_key_present("openai") is True


def _ready_stellar(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in ("stellar_agent_registry", "stellar_payment_escrow", "stellar_attestation_registry"):
        monkeypatch.setattr(settings, field, VALID_C)
    monkeypatch.setattr(settings, "stellar_asset_sac", VALID_C)
    monkeypatch.setattr(settings, "stellar_reputation_ledger", VALID_C)
    monkeypatch.setattr(settings, "stellar_admin_address", VALID_G)


def test_readiness_is_ready_on_claude_without_an_openai_key(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_stellar(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-secret-value")
    r = client.get("/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["llm"] == "ok"
    assert body["orchestrator"]["provider"] == "anthropic" and body["orchestrator"]["anthropic_key"] is True
    assert "sk-ant" not in r.text  # presence only, never the key


def test_readiness_is_not_ready_when_claude_is_forced_without_its_key(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_stellar(monkeypatch)
    monkeypatch.setattr(settings, "openai_api_key", "sk-openai")
    monkeypatch.setattr(settings, "orchestrator_provider", "anthropic")
    r = client.get("/readiness")
    assert r.status_code == 503 and r.json()["llm"] == "missing_key"


def test_a_missing_jev_key_never_gates_readiness(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready_stellar(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")
    r = client.get("/readiness")
    assert r.status_code == 200 and r.json()["orchestrator"]["typesafe_key"] is False


def test_readiness_reports_todays_spend_and_a_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_daily_spend_cap_usd", 0.01)
    asyncio.run(spend.record(model="claude-opus-5-5", purpose="planner", usage=Usage(), cost=0.0125))
    report = provider.readiness()
    assert (report.spend.spent_usd, report.spend.cap_usd, report.spend.paused) == (0.0125, 0.01, True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("orchestrator_provider", "gemini"),
        ("llm_daily_spend_cap_usd", -1.0),
        ("llm_daily_spend_cap_usd", float("nan")),
        ("llm_daily_spend_cap_usd", float("inf")),
        ("claude_timeout_seconds", 0.0),
        ("jev_timeout_seconds", float("inf")),
        ("claude_max_retries", -1),
        ("jev_max_retries", 11),
        ("claude_model_complex", " "),
        ("typesafe_model", ""),
    ],
)
def test_unusable_llm_settings_refuse_to_boot(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **{field: value})  # type: ignore[call-arg]
    assert field.upper() in str(caught.value)


def test_the_shipped_llm_settings_boot() -> None:
    shipped = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (shipped.orchestrator_provider, shipped.llm_daily_spend_cap_usd, shipped.typesafe_model) == (
        "",
        10.0,
        "jev-1.13.0",
    )


def test_close_releases_the_clients_and_the_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm import claude, jev

    closed: list[str] = []

    class Closable:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            closed.append(self.name)

    claude.set_transport(Closable("claude"))  # type: ignore[arg-type]
    jev.set_transport(Closable("jev"))  # type: ignore[arg-type]
    ledger = spend.get_ledger()
    asyncio.run(provider.close())
    assert closed == ["claude", "jev"] and spend.get_ledger() is not ledger


def test_the_app_shuts_the_model_layer_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    calls: list[str] = []

    async def close() -> None:
        calls.append("closed")

    monkeypatch.setattr(provider, "close", close)
    with TestClient(app):
        pass
    assert calls == ["closed"]
