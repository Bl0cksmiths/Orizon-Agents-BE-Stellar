"""
The platform treasury — the account the built-in agents are registered to on-chain.

Escrow v2 pays each payout to `AgentRegistry.owner_of(agent_id)`, read inside
`settle`. The built-in catalog (`agt_01h8` … `agt_12r0`) is registered on that
registry with the platform treasury as owner, so a delivered built-in step is
paid to the treasury (docs/decisions/0016-platform-treasury.md).

`register` is permissionless and write-once: whoever registers an id first
owns it for good, and nothing in the contract reserves the `agt_` namespace.
So the backend never trusts an `agt_` owner it reads: a built-in step is paid
only when `owner_of` names the treasury declared here, and the registry mirror
checks every built-in record against the terms this module derives from the
seeded catalog.

Where the treasury comes from
-----------------------------
The committed team register (`app/data/team_wallets.json`), the one entry
whose role is `TREASURY_ROLE`. One declaration serves both rules that need it:
the adoption report and the overview count it as the team's (it is in the
register), and settlement pays it (it has this role). No register entry means
no treasury, and then no built-in step is paid; two is refused, because the
backend could not say which one the agents were registered to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import money
from ..agents.registry import WORKERS, get_worker
from ..state import state

TREASURY_ROLE = "platform treasury (built-in agents' payee)"

# A Soroban Symbol: what `register` takes for an id and for each skill.
_SYMBOL_MAX_CHARS = 32
_NOT_SYMBOL = re.compile(r"[^A-Za-z0-9_]")


class TreasuryError(ValueError):
    """The team register does not name one platform treasury."""


def treasury_address() -> str | None:
    """The platform treasury's account, from the team register; None when none is declared.

    Read from `adoption_svc.TEAM_REGISTER` at call time (imported lazily: the
    adoption service imports the registry mirror, which imports this module),
    so a test that swaps the register sees its own.
    """
    from . import adoption_svc

    found = [w.address for w in adoption_svc.TEAM_REGISTER if w.role == TREASURY_ROLE]
    if len(found) > 1:
        raise TreasuryError(f"the team register has {len(found)} entries with the role {TREASURY_ROLE!r}")
    return found[0] if found else None


def is_built_in(agent_id: str) -> bool:
    """Whether `agent_id` runs on one of the platform's own workers.

    The same test `plannable()`, the plan's `executor` stamp and
    `resolve_worker` apply, so the agents this module pays the treasury for are
    exactly the ones a plan calls built-in.
    """
    return get_worker(agent_id) is not None


def built_in_ids() -> list[str]:
    """Every built-in agent's id, in catalog order."""
    return sorted(WORKERS)


def onchain_skill(skill: str) -> str:
    """`skill` as a Symbol: every character outside `[A-Za-z0-9_]` made `_`, cut to 32.

    The catalog's skills are display text (`translate.42` lists "42 langs"),
    and `register` takes `Vec<Symbol>`, which cannot hold a space.
    """
    return _NOT_SYMBOL.sub("_", skill)[:_SYMBOL_MAX_CHARS]


@dataclass(frozen=True)
class Registration:
    """What one built-in agent is registered with: `register(treasury, id, name, skills, price)`."""

    agent_id: str
    name: str
    skills: tuple[str, ...]
    price_stroops: int


def registration_for(agent_id: str) -> Registration:
    """The terms `agent_id` is registered with, from the seeded catalog in `state.agents`.

    The price is the one a plan freezes into `PlanStep.price_stroops`
    (`money.to_stroops` of the seeded price), so the on-chain record says what
    a plan charges for the agent. Raises KeyError when the catalog does not
    hold `agent_id`.
    """
    agent = state.agents[agent_id]
    return Registration(
        agent_id=agent.id,
        name=agent.name,
        skills=tuple(onchain_skill(s) for s in agent.skills),
        price_stroops=money.to_stroops(agent.price),
    )


def registrations() -> list[Registration]:
    """Every built-in agent's registration, in catalog order."""
    return [registration_for(agent_id) for agent_id in built_in_ids()]
