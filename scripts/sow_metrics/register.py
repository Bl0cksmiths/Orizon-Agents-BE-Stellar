"""The committed register of wallets the Blocksmiths control (`app/data/team_wallets.json`).

An agent owned by one of these wallets is not externally operated, whatever
its name says, and a charge it receives is the team paying the team. The
register is read in the one layout the backend commits and validates at boot,
`{"wallets": [{"address": "G...", "role": ...}]}`; only each entry's `address`
is a team account (an entry's `evidence` cites others, like the friendbot
that funded it, which are not ours). Any other layout, or an empty register,
is refused: a count against an empty register would call every wallet
external. Re-stated from `scripts/adoption_report/register.py` so the tools
stay independent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from stellar_sdk import StrKey


class RegisterError(Exception):
    """The register is missing, unreadable, or names no account."""


@dataclass(frozen=True)
class TeamRegister:
    path: Path
    roles: dict[str, str]  # address -> the role the register gives it

    def role(self, account: str) -> str | None:
        return self.roles.get(account)


def load(path: Path) -> TeamRegister:
    try:
        body = json.loads(path.read_bytes())
    except OSError as exc:
        raise RegisterError(f"team register {path} cannot be read: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise RegisterError(f"team register {path} is not JSON: {exc}") from exc
    wallets = body.get("wallets") if isinstance(body, dict) else None
    if not isinstance(wallets, list):
        raise RegisterError(f"team register {path} has no `wallets` list, the layout the backend commits")
    roles: dict[str, str] = {}
    for index, entry in enumerate(wallets):
        address = entry.get("address") if isinstance(entry, dict) else None
        if not isinstance(address, str) or not StrKey.is_valid_ed25519_public_key(address):
            raise RegisterError(f"team register {path} entry {index} has no valid Stellar account `address`")
        roles[address] = str(entry.get("role") or "team wallet")
    if not roles:
        raise RegisterError(f"team register {path} names no Stellar account; an empty register proves nothing")
    return TeamRegister(path=path, roles=roles)
