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
    wallets = node.get("wallets") if isinstance(node, dict) else None
    if not isinstance(wallets, list):
        raise RegisterError("team register has no `wallets` list, the layout the backend commits")
    found: set[str] = set()
    for index, entry in enumerate(wallets):
        address = entry.get("address") if isinstance(entry, dict) else None
        if not isinstance(address, str) or not StrKey.is_valid_ed25519_public_key(address):
            raise RegisterError(f"team register entry {index} has no valid Stellar account `address`")
        found.add(address)
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
