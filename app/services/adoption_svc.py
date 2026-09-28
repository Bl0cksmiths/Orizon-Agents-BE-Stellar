"""
Adoption evidence — SOW §6.3's three numbers, computed from on-chain facts.

§6.3 asks for at least two externally operated agents, at least two unique
operator wallets, and at least three workflows routed to external agents and
settled on testnet, each with a charge transaction a reviewer can open. The
story's rule is the whole design: "External means external. Wallets controlled
by the Blocksmiths do not count toward the metric under any framing." So this
module answers the question the way a sceptical reviewer would, and shows its
working so they never have to trust us:

  - the agent list is the on-chain AgentRegistry (the registry-synced mirror in
    `state.agents`, cross-checked against `list_ids`), never our seeded catalog;
  - an agent is external only when its on-chain owner is in NEITHER the
    committed team register (`app/data/team_wallets.json`) NOR the set of keys
    this deployment demonstrably holds at runtime, and every agent that fails
    that test is listed under `excluded` with the reason;
  - a settled workflow is a `charged` event `settlement_svc` already counts as
    verified revenue, carrying its transaction hash and a Stellar Expert link;
  - a read that could not be made is reported as unreadable and never as zero.

The team register
-----------------
A public, committed declaration of every account the team controls, with where
each one is documented. It is loaded and validated at import, so a malformed
register — an address that is not a G-strkey, a duplicate, a missing role or
evidence — stops the service booting with a message naming the entry, rather
than quietly letting one of our own wallets read as an outside operator.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from stellar_sdk import StrKey

logger = logging.getLogger(__name__)

# Beside the code that reads it, so it deploys with the service (render.yaml
# runs from the source checkout) and a reviewer finds it next to the rule.
REGISTER_PATH = Path(__file__).resolve().parent.parent / "data" / "team_wallets.json"

_ENTRY_FIELDS = frozenset({"address", "role", "evidence"})


class TeamRegisterError(ValueError):
    """The team register is unusable; the message names the offending entry."""


@dataclass(frozen=True)
class TeamWallet:
    """One declared team account: what it is for, and where that is documented."""

    address: str
    role: str
    evidence: str


def load_team_register(path: Path = REGISTER_PATH) -> tuple[TeamWallet, ...]:
    """Read and validate the register. Raises TeamRegisterError naming the entry.

    Strict on purpose. Every mistake this refuses has the same consequence if
    it is let through: a team wallet silently stops being recognised, and our
    own agent is counted as an outside operator's. An empty register is refused
    for the same reason — it would make every owner "external".
    """
    name = path.name
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise TeamRegisterError(f"{name}: unreadable: {e}") from e
    if not isinstance(raw, dict) or not isinstance(raw.get("wallets"), list):
        raise TeamRegisterError(f"{name}: expected an object with a `wallets` list")
    if not raw["wallets"]:
        raise TeamRegisterError(f"{name}: `wallets` is empty, which would make every owner external")

    first_seen: dict[str, int] = {}
    wallets: list[TeamWallet] = []
    for index, entry in enumerate(raw["wallets"]):
        label = f"{name} entry {index}"
        if not isinstance(entry, dict):
            raise TeamRegisterError(f"{label}: expected an object, got {type(entry).__name__}")
        if set(entry) != _ENTRY_FIELDS:
            raise TeamRegisterError(f"{label}: expected exactly address, role and evidence, got {sorted(entry)}")
        address = entry["address"]
        if not isinstance(address, str) or not StrKey.is_valid_ed25519_public_key(address):
            raise TeamRegisterError(f"{label}: {address!r} is not a valid Stellar account id (G-strkey)")
        label = f"{label} ({address})"
        for field in ("role", "evidence"):
            value = entry[field]
            if not isinstance(value, str) or not value.strip():
                raise TeamRegisterError(f"{label}: `{field}` must be a non-empty string")
        if address in first_seen:
            raise TeamRegisterError(f"{label}: duplicates entry {first_seen[address]}")
        first_seen[address] = index
        wallets.append(TeamWallet(address=address, role=entry["role"].strip(), evidence=entry["evidence"].strip()))
    return tuple(wallets)


# Loaded at import: the router imports this module and `app.main` imports the
# router, so a bad register refuses boot instead of serving a wrong count.
TEAM_REGISTER: tuple[TeamWallet, ...] = load_team_register()
