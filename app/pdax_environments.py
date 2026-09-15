"""PDAX environment table — the single source of truth for which environments
exist and which of them can move real value.

Deliberately dependency-free, and deliberately NOT inside the `app.pdax`
package: `app/config.py` needs this table while its own `Settings` object is
still being constructed, and importing anything under `app.pdax` would execute
that package's `__init__`, which imports `app.config` right back. Keeping the
table here means both sides can import it without a cycle — so the environment
list stops being duplicated (and able to drift) between the two modules.
"""

from __future__ import annotations

# Base URLs per environment (see PDAX "Getting Started").
BASE_URLS: dict[str, str] = {
    "production": "https://services.pdax.ph/api/pdax-api",
    "stage": "https://stage.services.sandbox.pdax.ph/api/pdax-api",
    "uat": "https://uat.services.sandbox.pdax.ph/api/pdax-api",
}

DEFAULT_ENVIRONMENT = "uat"

# Sandbox hosts settle play money; every other host settles real fiat.
_SANDBOX_MARKER = "sandbox"


def normalize(env: str | None) -> str:
    """Canonical form of an environment name — trimmed, lowercased, defaulted."""
    return (env or DEFAULT_ENVIRONMENT).strip().lower()


def base_url_for(env: str | None) -> str:
    """Resolve the base URL for `env`; raises on an unknown environment."""
    resolved = normalize(env)
    if resolved not in BASE_URLS:
        raise RuntimeError(f"unknown PDAX environment {resolved!r}; expected one of {list(BASE_URLS)}")
    return BASE_URLS[resolved]


def moves_real_value(env: str | None) -> bool:
    """True when `env` resolves to a base URL that settles real fiat.

    Derived from the resolved URL rather than a hardcoded environment name, so
    adding a real-money environment can never silently bypass the guards that
    depend on this (the API-key requirement and the webhook-signature
    requirement). An unknown environment is treated as NOT real-value: it
    cannot reach PDAX at all, because `base_url_for` refuses it.
    """
    return _SANDBOX_MARKER not in BASE_URLS.get(normalize(env), _SANDBOX_MARKER)
