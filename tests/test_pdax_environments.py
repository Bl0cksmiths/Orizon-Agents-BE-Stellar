"""The PDAX environment table — which environments exist, and which of them
settle real money.

The two fail-fast guards in app/config.py (the API-key requirement and the
webhook-signature requirement) key off `moves_real_value`, so what matters here
is that it is derived from the *resolved base URL* — sandbox hosts are play
money — rather than from a hardcoded environment name. Derived that way, adding
a real-money environment cannot silently slip past those guards.
"""

from __future__ import annotations

import pytest

from app.pdax_environments import BASE_URLS, base_url_for, moves_real_value, normalize


def test_only_production_settles_real_money() -> None:
    assert moves_real_value("production") is True
    assert moves_real_value("stage") is False
    assert moves_real_value("uat") is False


def test_real_value_is_derived_from_the_url_not_the_name() -> None:
    # It is the sandbox host — not the literal name — that makes an
    # environment play money. Pin that for every entry in the table.
    for env, url in BASE_URLS.items():
        assert moves_real_value(env) is ("sandbox" not in url), env


def test_environment_names_are_normalized() -> None:
    assert normalize("  PRODUCTION ") == "production"
    assert moves_real_value("  PRODUCTION ") is True


def test_unknown_environment_is_never_treated_as_real_value() -> None:
    # base_url_for refuses it, so it can never reach PDAX at all — and it must
    # not be mistaken for a real-money target by the guards on the way there.
    assert moves_real_value("bogus") is False
    with pytest.raises(RuntimeError):
        base_url_for("bogus")


def test_the_default_environment_is_a_sandbox() -> None:
    assert moves_real_value(None) is False
    assert "sandbox" in base_url_for(None)
