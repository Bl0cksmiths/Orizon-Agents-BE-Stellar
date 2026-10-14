"""The startup reports in `Settings`: what each one says, where, and how often.

`tests/test_config_validators.py` proves each report fires and stays quiet on
good values. These pin the rest of the contract an operator reads in a deploy
log — the exact sentence, the `app.config` logger, ERROR level, one line per
construction — so that moving the reports between pydantic hooks cannot
change any of it.

Constructed with _env_file=None so the local .env can never leak into
assertions.
"""

from __future__ import annotations

import logging

import pytest

from app.config import PDAX_ENVIRONMENTS, Settings

CONFIG_LOGGER = "app.config"

_UNKNOWN_PDAX_ENVIRONMENT = (
    "PDAX_ENVIRONMENT='produciton' is not a known PDAX environment (expected one of: "
    + ", ".join(PDAX_ENVIRONMENTS)
    + "). No base URL can be resolved, so every /api/pdax/* route will fail on its "
    "first call; an unrecognised value is also not treated as production, so the "
    "webhook-signature requirement will not be enforced. Fix PDAX_ENVIRONMENT."
)
_MALFORMED_OTP_SECRET = (
    "PDAX_OTP_SECRET is not valid base32. Every PDAX login that hits a "
    "SOFTWARE_TOKEN_MFA challenge will fail to answer it, so no PDAX call can "
    "authenticate. Re-copy the TOTP seed from the PDAX console, or unset "
    "PDAX_OTP_SECRET if the account has no MFA."
)
_MALFORMED_SIGNING_KEY = (
    "STELLAR_SIGNING_KEY is malformed — it is neither a valid S… secret key nor a "
    "valid 12/24-word mnemonic phrase (the value is withheld from this log). Every "
    "backend-signed transaction will fail, so /api/stellar/server/charge and "
    "/server/seal will answer 400 charge_failed. Re-inject the key from host secrets."
)
_MISSING_ADMIN_ADDRESS = (
    "STELLAR_ADMIN_ADDRESS is not set, but contract ids are configured (STELLAR_PAYMENT_ESCROW). Contract "
    "reads have no source account, so every on-chain read will fail with "
    "'no source address; set STELLAR_ADMIN_ADDRESS' and /readiness will answer 503 "
    "not_ready. Set STELLAR_ADMIN_ADDRESS to the G… address that funds simulation."
)

# One misconfiguration per report, each paired with the line it must log.
_REPORTED = {
    "unknown PDAX_ENVIRONMENT": ({"pdax_environment": "produciton"}, _UNKNOWN_PDAX_ENVIRONMENT),
    "malformed PDAX_OTP_SECRET": ({"pdax_otp_secret": "not-base32!!"}, _MALFORMED_OTP_SECRET),
    "malformed STELLAR_SIGNING_KEY": ({"stellar_signing_key": "SBADKEY" + "Z" * 49}, _MALFORMED_SIGNING_KEY),
    "missing STELLAR_ADMIN_ADDRESS": (
        {
            "stellar_payment_escrow": "CBJCQBA47Q3EQ7HC46GAWJPVM7KMD5KAEI5KG4FPYJFKR3NYB4QR5CNF",
            "stellar_admin_address": "",
        },
        _MISSING_ADMIN_ADDRESS,
    ),
}

# The good value for each report's variable, which must log nothing.
_QUIET = {
    "known PDAX_ENVIRONMENT": {"pdax_environment": "stage"},
    "base32 PDAX_OTP_SECRET": {"pdax_otp_secret": "JBSWY3DPEHPK3PXP"},
    "parseable STELLAR_SIGNING_KEY": {
        "stellar_signing_key": "illness spike retreat truth genius clock brain pass fit cave bargain toe"
    },
    "STELLAR_ADMIN_ADDRESS beside the contract ids": {
        "stellar_payment_escrow": "CBJCQBA47Q3EQ7HC46GAWJPVM7KMD5KAEI5KG4FPYJFKR3NYB4QR5CNF",
        "stellar_admin_address": "GA7AI5TAJEZA27I666DSJC4MUJYBEWUYNNZWPU7R2ONA7IZQVO6R5OQV",
    },
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def _config_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == CONFIG_LOGGER]


@pytest.mark.parametrize(("overrides", "line"), list(_REPORTED.values()), ids=list(_REPORTED))
def test_each_report_logs_its_exact_line_once_at_error_and_still_boots(caplog, overrides, line):
    with caplog.at_level(logging.DEBUG, logger=CONFIG_LOGGER):
        built = _settings(**overrides)

    assert isinstance(built, Settings)  # reported, never refused
    [record] = _config_records(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == line
    assert record.exc_info is None


@pytest.mark.parametrize(("overrides", "line"), list(_REPORTED.values()), ids=list(_REPORTED))
def test_each_construction_reports_again(caplog, overrides, line):
    # One line per Settings(), not one per process: a test (or a reload) that
    # builds the settings twice has to see the report twice.
    with caplog.at_level(logging.DEBUG, logger=CONFIG_LOGGER):
        _settings(**overrides)
        _settings(**overrides)

    assert [r.getMessage() for r in _config_records(caplog)] == [line, line]


@pytest.mark.parametrize("overrides", list(_QUIET.values()), ids=list(_QUIET))
def test_a_good_value_logs_nothing_at_all(caplog, overrides):
    with caplog.at_level(logging.DEBUG, logger=CONFIG_LOGGER):
        built = _settings(**overrides)

    assert isinstance(built, Settings)
    assert _config_records(caplog) == []


def test_every_report_fires_together_in_declaration_order(caplog):
    merged: dict[str, object] = {}
    for overrides, _line in _REPORTED.values():
        merged.update(overrides)

    with caplog.at_level(logging.DEBUG, logger=CONFIG_LOGGER):
        _settings(**merged)

    assert [r.getMessage() for r in _config_records(caplog)] == [line for _o, line in _REPORTED.values()]
