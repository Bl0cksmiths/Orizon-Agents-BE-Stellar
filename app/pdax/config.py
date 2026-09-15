"""
PDAX environment resolution.

The PDAX institutions API runs in three environments. The base URL is
selected from `settings.pdax_environment` ("production" | "stage" | "uat").
All endpoint paths are versioned under `/pdax-institution/v1` (a few under
`/v2`); see app/pdax/client.py for how paths are joined.

The environment table itself lives in `app/pdax_environments.py` — outside this
package, so `app/config.py` can share it without an import cycle. This module
is the settings-bound view of it.
"""

from __future__ import annotations

from ..config import settings
from ..pdax_environments import (
    BASE_URLS,
    DEFAULT_ENVIRONMENT,
    base_url_for,
    normalize,
)
from ..pdax_environments import moves_real_value as _env_moves_real_value

__all__ = [
    "BASE_URLS",
    "DEFAULT_ENVIRONMENT",
    "allow_unsigned_webhooks",
    "base_url",
    "is_production",
    "moves_real_value",
]


def base_url() -> str:
    """Resolve the PDAX base URL for the configured environment."""
    return base_url_for(settings.pdax_environment)


def is_production() -> bool:
    return normalize(settings.pdax_environment) == "production"


def moves_real_value() -> bool:
    """True when the configured environment settles real fiat rather than
    sandbox play money — derived from the resolved base URL, not the name."""
    return _env_moves_real_value(settings.pdax_environment)


def allow_unsigned_webhooks() -> bool:
    """Escape hatch for local dev/smoke: accept inbound webhooks without a
    signature when `PDAX_WEBHOOK_SECRET` is unset. Defaults to false, so
    webhook verification fails closed. Set PDAX_ALLOW_UNSIGNED_WEBHOOKS=true
    (declared on Settings, so it is validated with the rest of the config)
    to opt out — never in production."""
    return bool(getattr(settings, "pdax_allow_unsigned_webhooks", False))
