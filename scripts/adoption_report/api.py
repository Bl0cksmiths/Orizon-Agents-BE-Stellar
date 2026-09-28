"""`GET /api/ecosystem/adoption`: fetched once, parsed against the frozen shape.

The answer is a list of CLAIMS, nothing more. This module only checks that the
claims are well formed — every key present, every value the type the frozen
shape names — and turns them into dataclasses. Whether any of them is TRUE is
`verify.py`'s question, answered from the ledger.

A shape error names the exact path that is wrong (`operators[1].agents[0].bound:
expected bool, got str`), because "the API answered something odd" is not a
message anyone can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import ADOPTION_PATH, EXCLUSION_REASONS, TARGETS
from .retry import RETRYABLE_STATUS, RetryableStatus, RetryPolicy, retry_after_seconds


class ShapeError(Exception):
    """The endpoint answered, and the answer is not the frozen shape."""


class ApiUnreachable(Exception):
    """The endpoint did not give a usable answer at all."""


@dataclass(frozen=True)
class WorkflowClaim:
    job_id_hex: str
    tx_hash: str
    explorer: str
    amount_usdc: float
    payer: str
    settled_at: float


@dataclass(frozen=True)
class AgentClaim:
    agent_id: str
    name: str
    active: bool
    bound: bool
    settled_workflows: list[WorkflowClaim] = field(default_factory=list)


@dataclass(frozen=True)
class OperatorClaim:
    owner: str
    owner_explorer: str
    agents: list[AgentClaim] = field(default_factory=list)


@dataclass(frozen=True)
class ExclusionClaim:
    owner: str
    owner_explorer: str
    reason: str
    role: str
    agent_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdoptionClaim:
    network: str
    generated_at: float
    targets: dict[str, int]
    totals: dict[str, int]
    met: dict[str, bool]
    operators: list[OperatorClaim]
    excluded: list[ExclusionClaim]
    degraded: bool
    unreadable_agents: list[Any]


# ── shape ───────────────────────────────────────────────────────
def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__


def _get(obj: Any, key: str, path: str) -> Any:
    if not isinstance(obj, dict):
        raise ShapeError(f"{path or 'the body'}: expected an object, got {_type_name(obj)}")
    if key not in obj:
        raise ShapeError(f"{path + '.' if path else ''}{key}: missing")
    return obj[key]


def _str(obj: Any, key: str, path: str) -> str:
    value = _get(obj, key, path)
    if not isinstance(value, str):
        raise ShapeError(f"{path + '.' if path else ''}{key}: expected str, got {_type_name(value)}")
    return value


def _bool(obj: Any, key: str, path: str) -> bool:
    value = _get(obj, key, path)
    if not isinstance(value, bool):
        raise ShapeError(f"{path + '.' if path else ''}{key}: expected bool, got {_type_name(value)}")
    return value


def _number(obj: Any, key: str, path: str) -> float:
    value = _get(obj, key, path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShapeError(f"{path + '.' if path else ''}{key}: expected a number, got {_type_name(value)}")
    return float(value)


def _list(obj: Any, key: str, path: str) -> list[Any]:
    value = _get(obj, key, path)
    if not isinstance(value, list):
        raise ShapeError(f"{path + '.' if path else ''}{key}: expected a list, got {_type_name(value)}")
    return value


def _metric_block(obj: Any, key: str, kind: type) -> dict[str, Any]:
    block = _get(obj, key, "")
    if not isinstance(block, dict):
        raise ShapeError(f"{key}: expected an object, got {_type_name(block)}")
    out: dict[str, Any] = {}
    for metric in TARGETS:
        value = _get(block, metric, key)
        if kind is bool:
            if not isinstance(value, bool):
                raise ShapeError(f"{key}.{metric}: expected bool, got {_type_name(value)}")
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ShapeError(f"{key}.{metric}: expected int, got {_type_name(value)}")
        out[metric] = value
    return out


def parse(body: Any) -> AdoptionClaim:
    """The frozen shape, or a `ShapeError` naming the first path that breaks it."""
    operators: list[OperatorClaim] = []
    for i, raw_op in enumerate(_list(body, "operators", "")):
        op_path = f"operators[{i}]"
        agents: list[AgentClaim] = []
        for j, raw_agent in enumerate(_list(raw_op, "agents", op_path)):
            a_path = f"{op_path}.agents[{j}]"
            workflows: list[WorkflowClaim] = []
            for k, raw_wf in enumerate(_list(raw_agent, "settled_workflows", a_path)):
                w_path = f"{a_path}.settled_workflows[{k}]"
                workflows.append(
                    WorkflowClaim(
                        job_id_hex=_str(raw_wf, "job_id_hex", w_path),
                        tx_hash=_str(raw_wf, "tx_hash", w_path),
                        explorer=_str(raw_wf, "explorer", w_path),
                        amount_usdc=_number(raw_wf, "amount_usdc", w_path),
                        payer=_str(raw_wf, "payer", w_path),
                        settled_at=_number(raw_wf, "settled_at", w_path),
                    )
                )
            agents.append(
                AgentClaim(
                    agent_id=_str(raw_agent, "agent_id", a_path),
                    name=_str(raw_agent, "name", a_path),
                    active=_bool(raw_agent, "active", a_path),
                    bound=_bool(raw_agent, "bound", a_path),
                    settled_workflows=workflows,
                )
            )
        operators.append(
            OperatorClaim(
                owner=_str(raw_op, "owner", op_path),
                owner_explorer=_str(raw_op, "owner_explorer", op_path),
                agents=agents,
            )
        )

    excluded: list[ExclusionClaim] = []
    for i, raw_ex in enumerate(_list(body, "excluded", "")):
        ex_path = f"excluded[{i}]"
        reason = _str(raw_ex, "reason", ex_path)
        if reason not in EXCLUSION_REASONS:
            raise ShapeError(f"{ex_path}.reason: {reason!r} is not one of {sorted(EXCLUSION_REASONS)}")
        agent_ids = _list(raw_ex, "agent_ids", ex_path)
        for j, agent_id in enumerate(agent_ids):
            if not isinstance(agent_id, str):
                raise ShapeError(f"{ex_path}.agent_ids[{j}]: expected str, got {_type_name(agent_id)}")
        excluded.append(
            ExclusionClaim(
                owner=_str(raw_ex, "owner", ex_path),
                owner_explorer=_str(raw_ex, "owner_explorer", ex_path),
                reason=reason,
                role=_str(raw_ex, "role", ex_path),
                agent_ids=list(agent_ids),
            )
        )

    return AdoptionClaim(
        network=_str(body, "network", ""),
        generated_at=_number(body, "generated_at", ""),
        targets=_metric_block(body, "targets", int),
        totals=_metric_block(body, "totals", int),
        met=_metric_block(body, "met", bool),
        operators=operators,
        excluded=excluded,
        degraded=_bool(body, "degraded", ""),
        unreadable_agents=_list(body, "unreadable_agents", ""),
    )


# ── transport ───────────────────────────────────────────────────
@dataclass
class AdoptionApi:
    client: httpx.Client
    base: str
    retry: RetryPolicy

    @property
    def url(self) -> str:
        return f"{self.base}{ADOPTION_PATH}"

    def fetch(self) -> Any:
        """The endpoint's JSON body. A read, so retried on the transient statuses."""

        def once() -> Any:
            response = self.client.get(self.url, timeout=90.0, headers={"accept": "application/json"})
            if response.status_code in RETRYABLE_STATUS:
                raise RetryableStatus(response.status_code, retry_after_seconds(response))
            if response.status_code != 200:
                raise ApiUnreachable(f"GET {self.url} answered HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError as exc:
                raise ApiUnreachable(f"GET {self.url} answered 200 with a body that is not JSON") from exc

        try:
            return self.retry.run(once)
        except (httpx.HTTPError, RetryableStatus) as exc:
            raise ApiUnreachable(f"GET {self.url} failed after retries: {exc}") from exc
