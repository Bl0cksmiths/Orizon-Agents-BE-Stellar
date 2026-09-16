"""Marketplace reads of the agent registry.

`bound` is stamped HERE, at read time, and never onto the `Agent` held in
`state.agents`. The stored copy is written by two things that run on their own
schedule — `seed` once at boot, and `registry_sync`'s poll of the chain — while
a binding changes whenever an operator binds, rebinds or repoints an endpoint.
Stamping at write time would therefore publish a `bound` that is correct only
until the next bind and not refreshed until the next sync pass, and a seeded
agent, which no pass ever revisits, would carry its boot-time answer forever.
Read time has no such window: the answer is computed for the request that asks.

It is also the cheap side. `binding_registry` keeps the bound ids in an
in-memory set, so this is one O(1) lookup per agent with no I/O, on a list the
page already fetches — which is the whole reason the field exists rather than
an endpoint the client would have to call once per agent.

Stamping is a COPY, never a mutation: `state.list_agents()` hands out the live
objects, so assigning to them would write this response's view back into
application state and recreate the staleness this avoids.
"""

from fastapi import APIRouter, HTTPException

from ..schemas import Agent
from ..services.binding_registry import is_bound
from ..state import state

router = APIRouter(tags=["agents"])


def _with_bound(agent: Agent) -> Agent:
    """`agent` with `bound` answered for its provenance.

    A seeded agent is always None. It runs on a worker inside this process and
    has no endpoint to bind, so `False` would report a defect where there is
    none — the conflation the client's `needsBinding` exists to prevent. Only an
    on-chain agent can be meaningfully bound or unbound, and `is_bound` answers
    None for one of those too, when the bound set has not been loaded and we
    have nothing to report but our own ignorance.
    """
    bound = is_bound(agent.id) if agent.source == "onchain" else None
    return agent.model_copy(update={"bound": bound})


@router.get("/agents", response_model=list[Agent], summary="List registered agents")
async def list_agents() -> list[Agent]:
    return [_with_bound(a) for a in state.list_agents()]


@router.get("/agents/{agent_id}", response_model=Agent, summary="Get one agent by id")
async def get_agent(agent_id: str) -> Agent:
    agent = state.agents.get(agent_id)
    if agent is None:
        raise HTTPException(404, f"unknown agent: {agent_id}")
    return _with_bound(agent)
