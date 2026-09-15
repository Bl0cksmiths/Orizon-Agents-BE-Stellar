"""Settings validators, in two families.

Fail-fast: half-flipped or unsafe configs must refuse to boot instead of
running open. Report-only: values that are merely wrong log a named, actionable
line at startup and still boot — this service is live on mainnet with
autoDeploy on, so a wrong rejection would take the product down.

Constructed with _env_file=None so the local .env can never leak into
assertions."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError
from stellar_sdk import Keypair

from app.config import MAINNET_PASSPHRASE, PDAX_ENVIRONMENTS, Settings

CONFIG_LOGGER = "app.config"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == CONFIG_LOGGER and r.levelno >= logging.ERROR]


def test_defaults_are_safe_and_timeouts_declared():
    s = _settings()
    assert s.pdax_allow_unsigned_webhooks is False
    assert s.llm_timeout_seconds == 120.0
    assert s.decompose_timeout_seconds == 90.0


def test_mainnet_requires_mainnet_passphrase():
    with pytest.raises(ValidationError, match="STELLAR_NETWORK_PASSPHRASE"):
        _settings(stellar_network="mainnet")
    s = _settings(stellar_network="mainnet", stellar_network_passphrase=MAINNET_PASSPHRASE)
    assert s.stellar_network == "mainnet"


def test_production_rejects_unsigned_webhook_escape_hatch():
    with pytest.raises(ValidationError, match="PDAX_ALLOW_UNSIGNED_WEBHOOKS"):
        _settings(
            pdax_environment="production",
            pdax_webhook_secret="whsec",
            pdax_allow_unsigned_webhooks=True,
        )


def test_production_requires_webhook_secret():
    with pytest.raises(ValidationError, match="PDAX_WEBHOOK_SECRET"):
        _settings(pdax_environment="production", pdax_webhook_secret="")


def test_production_with_secret_and_signing_enforced_boots():
    s = _settings(pdax_environment="production", pdax_webhook_secret="whsec")
    assert s.pdax_webhook_secret == "whsec"


def test_non_production_environments_keep_the_escape_hatch():
    s = _settings(pdax_environment="uat", pdax_allow_unsigned_webhooks=True)
    assert s.pdax_allow_unsigned_webhooks is True


# ── API_KEY vs money-capable credentials ────────────────────────
# An empty API_KEY makes require_api_key a no-op, which is fine for the open
# demo and fatal once the process can sign mainnet transactions or move real
# fiat. These pin both halves: refuse the unsafe combinations, keep every
# demo/dev/read-only combination bootable.

_MAINNET = {"stellar_network": "mainnet", "stellar_network_passphrase": MAINNET_PASSPHRASE}
_SIGNER = "S" + "A" * 55


def test_mainnet_signer_without_api_key_refuses_to_boot():
    with pytest.raises(ValidationError, match="API_KEY is required"):
        _settings(**_MAINNET, stellar_signing_key=_SIGNER)


def test_mainnet_signer_with_api_key_boots():
    s = _settings(**_MAINNET, stellar_signing_key=_SIGNER, api_key="k")
    assert s.api_key == "k"


def test_public_network_alias_is_covered():
    # STELLAR_NETWORK=public is the same live network under another name.
    with pytest.raises(ValidationError, match="API_KEY is required"):
        _settings(
            stellar_network="public",
            stellar_network_passphrase=MAINNET_PASSPHRASE,
            stellar_signing_key=_SIGNER,
        )


def test_readonly_mainnet_without_signer_stays_open():
    # No signing key → /server/charge and /server/seal cannot sign anything,
    # so the open demo default is still safe.
    s = _settings(**_MAINNET)
    assert s.api_key == ""


def test_testnet_signer_does_not_require_api_key():
    # Local dev signs testnet play money; requiring a key here would break it.
    s = _settings(stellar_signing_key=_SIGNER)
    assert s.stellar_network == "testnet"
    assert s.api_key == ""


def test_production_pdax_credentials_without_api_key_refuse_to_boot():
    with pytest.raises(ValidationError, match="API_KEY is required"):
        _settings(
            pdax_environment="production",
            pdax_webhook_secret="whsec",
            pdax_username="ops@example.com",
            pdax_password="pw",
        )


def test_production_pdax_credentials_with_api_key_boot():
    s = _settings(
        pdax_environment="production",
        pdax_webhook_secret="whsec",
        pdax_username="ops@example.com",
        pdax_password="pw",
        api_key="k",
    )
    assert s.pdax_environment == "production"


def test_stage_pdax_credentials_do_not_require_api_key():
    s = _settings(pdax_environment="stage", pdax_username="ops@example.com", pdax_password="pw")
    assert s.api_key == ""


def test_error_names_every_exposure():
    with pytest.raises(ValidationError) as info:
        _settings(
            **_MAINNET,
            stellar_signing_key=_SIGNER,
            pdax_environment="production",
            pdax_webhook_secret="whsec",
            pdax_username="ops@example.com",
            pdax_password="pw",
        )
    message = str(info.value)
    assert "STELLAR_SIGNING_KEY" in message
    assert "PDAX" in message


def test_demo_defaults_still_boot_without_an_api_key():
    assert _settings().api_key == ""


# ── PDAX_ENVIRONMENT typo ───────────────────────────────────────
# app/pdax/config.py:base_url() raises lazily on the first client build, so a
# typo boots clean and 500s every PDAX route. Reported at startup; never fatal.


def test_pdax_environment_constant_matches_the_module_that_resolves_it():
    # Both sides now read the one table in app/pdax_environments.py (it lives
    # outside the app.pdax package precisely so app/config.py can import it),
    # so this pins that they still resolve to the same set.
    from app.pdax.config import BASE_URLS

    assert set(PDAX_ENVIRONMENTS) == set(BASE_URLS)


def test_unknown_pdax_environment_is_reported_and_still_boots(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        s = _settings(pdax_environment="produciton")
    assert s.pdax_environment == "produciton"  # boots — never fatal
    assert any("PDAX_ENVIRONMENT" in m and "produciton" in m for m in _errors(caplog))


def test_known_pdax_environments_are_quiet(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        for environment in PDAX_ENVIRONMENTS:
            extra = {"pdax_webhook_secret": "whsec"} if environment == "production" else {}
            _settings(pdax_environment=environment, **extra)
    assert _errors(caplog) == []


def test_pdax_environment_casing_and_whitespace_are_accepted(caplog):
    # base_url() strips and lowercases before lookup; the report must agree.
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(pdax_environment="  UAT ")
    assert _errors(caplog) == []


def test_empty_pdax_environment_is_quiet(caplog):
    # base_url() falls back to DEFAULT_ENVIRONMENT when unset.
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(pdax_environment="")
    assert _errors(caplog) == []


# ── PDAX_OTP_SECRET malformed ───────────────────────────────────
# app/pdax/totp.py base32-decodes the seed at the first MFA challenge.
# Reported at startup; never fatal, and the seed itself is never echoed.

_VALID_OTP_SECRET = "JBSWY3DPEHPK3PXP"


def test_malformed_otp_secret_is_reported_and_still_boots(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        s = _settings(pdax_otp_secret="not-base32!!")
    assert s.pdax_otp_secret == "not-base32!!"  # boots — never fatal
    assert any("PDAX_OTP_SECRET" in m for m in _errors(caplog))


def test_malformed_otp_secret_report_never_echoes_the_seed(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(pdax_otp_secret="not-base32!!")
    assert _errors(caplog)
    assert all("not-base32" not in m for m in _errors(caplog))


def test_valid_otp_secret_is_quiet(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(pdax_otp_secret=_VALID_OTP_SECRET)
    assert _errors(caplog) == []


def test_otp_secret_padding_matches_the_totp_module(caplog):
    # An unpadded, lowercased, space-separated seed is what a console copy
    # looks like; totp.py accepts it, so the startup report must too.
    from app.pdax.totp import totp_now

    unpadded = "jbsw y3dp ehpk 3pxp"
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(pdax_otp_secret=unpadded)
    assert _errors(caplog) == []
    assert totp_now(unpadded, timestamp=0) == totp_now(_VALID_OTP_SECRET, timestamp=0)


def test_unset_otp_secret_is_quiet(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(pdax_otp_secret="")
    assert _errors(caplog) == []


# ── STELLAR_SIGNING_KEY malformed ───────────────────────────────
# app/stellar/client.py:_signer_keypair() parses the key lazily, so a typo
# first surfaces as a 400 charge_failed on a real payment. Reported at
# startup; never fatal, and the key is never echoed.

# Deterministic throwaway keypair — a literal S… string in the repo would trip
# secret scanners, and this proves the good-value path with a real key.
_REAL_SIGNER = Keypair.from_raw_ed25519_seed(bytes(range(32)))
_BAD_SIGNER = "SBADKEY" + "Z" * 49


def test_malformed_signing_key_is_reported_and_still_boots(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        s = _settings(stellar_signing_key=_BAD_SIGNER)
    assert s.stellar_signing_key == _BAD_SIGNER  # boots — never fatal
    assert any("STELLAR_SIGNING_KEY" in m for m in _errors(caplog))


def test_signing_key_report_never_echoes_the_secret(caplog):
    # The SDK's own error quotes the rejected seed back; nothing derived from
    # it may reach the log, in whole or in part.
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(stellar_signing_key=_BAD_SIGNER)
    messages = _errors(caplog)
    assert messages
    for message in messages:
        assert _BAD_SIGNER not in message
        for start in range(0, len(_BAD_SIGNER) - 7):
            assert _BAD_SIGNER[start : start + 8] not in message
    assert not [r for r in caplog.records if r.name == CONFIG_LOGGER and r.exc_info]


def test_valid_secret_key_is_quiet(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        s = _settings(stellar_signing_key=_REAL_SIGNER.secret)
    assert _errors(caplog) == []
    assert s.stellar_signing_key == _REAL_SIGNER.secret


def test_valid_mnemonic_is_quiet(caplog):
    # _signer_keypair() also accepts a 12/24-word BIP-39 phrase; the startup
    # report must accept everything that function can build.
    phrase = "illness spike retreat truth genius clock brain pass fit cave bargain toe"
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(stellar_signing_key=phrase)
    assert _errors(caplog) == []
    assert Keypair.from_mnemonic_phrase(phrase)


def test_malformed_mnemonic_is_reported(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(stellar_signing_key=" ".join(["notaword"] * 12))
    assert any("STELLAR_SIGNING_KEY" in m for m in _errors(caplog))


def test_unset_signing_key_is_quiet(caplog):
    # Read-only deployments are legitimate and must stay silent.
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(stellar_signing_key="")
    assert _errors(caplog) == []


# ── STELLAR_ADMIN_ADDRESS missing ───────────────────────────────
# Every contract read needs a source account; app/stellar/client.py raises
# "no source address" without one. /readiness already reports this as
# not_ready — the startup report must agree with it, not contradict it.

# render.yaml's live mainnet values: public ids, and the shape the deployed
# service actually boots with.
_PRODUCTION_CONTRACTS = {
    "stellar_agent_registry": "CBTJ3BXTMTA2PQLRTSAZHEWQRTBMNHYCOKY5WOIYAH36LT4HTN63LTD4",
    "stellar_reputation_ledger": "CDFWQJY72GPH7PEQVFGBDZESZNVRF6LQLVWU42CFMWPGRME5RWN5AXSX",
    "stellar_payment_escrow": "CBJCQBA47Q3EQ7HC46GAWJPVM7KMD5KAEI5KG4FPYJFKR3NYB4QR5CNF",
    "stellar_attestation_registry": "CBLV6QGFCMXBXHT62JZ7YH22NXW7MVBGV6TGOGX3OHY46GQGPYCTAAK4",
    "stellar_asset_sac": "CAS3J7GYLGXMF6TDJBBYYSE3HQ6BBSMLNUQ34T6TZMYMW2EVH34XOWMA",
}
_PRODUCTION_ADMIN = "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV"


def test_missing_admin_address_with_contract_ids_is_reported_and_still_boots(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        s = _settings(**_PRODUCTION_CONTRACTS, stellar_admin_address="")
    assert s.stellar_admin_address == ""  # boots — never fatal
    reported = _errors(caplog)
    assert any("STELLAR_ADMIN_ADDRESS" in m for m in reported)
    # Actionable means naming which ids are live, not just that some are.
    assert any("STELLAR_PAYMENT_ESCROW" in m for m in reported)


def test_admin_address_set_is_quiet(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(**_PRODUCTION_CONTRACTS, stellar_admin_address=_PRODUCTION_ADMIN)
    assert _errors(caplog) == []


def test_missing_admin_address_without_contract_ids_is_quiet(caplog):
    # No ids configured → nothing to read, and /readiness already calls the
    # deployment incomplete on the ids alone. Staying silent keeps the demo
    # and local dev boot clean.
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        _settings(stellar_admin_address="")
    assert _errors(caplog) == []


def test_missing_admin_address_agrees_with_the_readiness_probe(client, monkeypatch):
    # The startup report must not contradict /readiness (app/main.py): the
    # config it stays quiet about is exactly the config that answers ready.
    from app.config import settings as live

    for name, value in _PRODUCTION_CONTRACTS.items():
        monkeypatch.setattr(live, name, value)
    monkeypatch.setattr(live, "stellar_admin_address", _PRODUCTION_ADMIN)
    assert client.get("/readiness").status_code == 200

    monkeypatch.setattr(live, "stellar_admin_address", "")
    response = client.get("/readiness")
    assert response.status_code == 503
    assert response.json()["stellar"] == "incomplete"


# ── The live deployment must always boot ────────────────────────
# render.yaml sets autoDeploy, so a validator that rejects (or now, one that
# flags) the real mainnet config takes the product down on merge.


def test_demo_defaults_report_nothing(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        s = _settings()
    assert _errors(caplog) == []
    assert s.pdax_environment == "uat"


def _production_shaped(**overrides) -> Settings:
    return _settings(
        **_MAINNET,
        **_PRODUCTION_CONTRACTS,
        stellar_rpc_url="https://mainnet.sorobanrpc.com",
        stellar_admin_address=_PRODUCTION_ADMIN,
        stellar_signing_key=_REAL_SIGNER.secret,
        api_key="live-key",
        openai_api_key="sk-live",
        pdax_username="ops@example.com",
        pdax_password="pw",
        pdax_otp_secret=_VALID_OTP_SECRET,
        pdax_webhook_secret="whsec",
        **overrides,
    )


def test_production_shaped_config_boots_clean_and_silent(caplog):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        s = _production_shaped(pdax_environment="stage")  # render.yaml's value
    assert s.stellar_network == "mainnet"
    assert s.stellar_signing_key == _REAL_SIGNER.secret
    assert _errors(caplog) == []


def test_production_pdax_shaped_config_boots_clean_and_silent(caplog):
    # The same service once PDAX is flipped to production.
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        s = _production_shaped(pdax_environment="production")
    assert s.pdax_environment == "production"
    assert _errors(caplog) == []
