"""The committed register of wallets the Blocksmiths control.

Story 5.02's metric rule is that external means wallets the team does NOT
control, so an external owner that appears in this register is a failed claim,
however the API framed it.

The register's exact layout belongs to the backend lane that commits it, so it
is read by the one rule that cannot drift with that layout: EVERY Stellar
account strkey (`G...`, checksum-valid) named anywhere in the file, at any
depth, as a key or a value, is a team account. A register that names none is
refused, because a check against an empty register proves nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stellar_sdk import StrKey


class RegisterError(Exception):
    """The register is missing, unreadable, or names no account."""


@dataclass(frozen=True)
class TeamRegister:
    path: Path
    sha256: str
    accounts: frozenset[str]


def _accounts(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, str):
        if StrKey.is_valid_ed25519_public_key(node):
            found.add(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            found |= _accounts(key)
            found |= _accounts(value)
    elif isinstance(node, list):
        for value in node:
            found |= _accounts(value)
    return found


def load(path: Path) -> TeamRegister:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RegisterError(f"team register {path} cannot be read: {exc.strerror or exc}") from exc
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise RegisterError(f"team register {path} is not JSON: {exc}") from exc
    accounts = _accounts(body)
    if not accounts:
        raise RegisterError(f"team register {path} names no Stellar account; an empty register proves nothing")
    return TeamRegister(path=path, sha256=hashlib.sha256(raw).hexdigest(), accounts=frozenset(accounts))
