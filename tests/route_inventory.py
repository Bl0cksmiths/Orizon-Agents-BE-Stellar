"""The app's state-changing operations, and which operator key guards each — read off its OpenAPI.

Two test files need "every write route" and "is it behind the operator key":
the authorization matrix and the per-route budgets. Walking `app.routes`
answered both until FastAPI 0.140, which stopped flattening included routers
into the parent's route list (they arrive as one nested entry each, with the
include's prefix and dependencies kept beside them, not merged in). A walk
that knows that shape breaks again on the next one.

The published schema is the stable answer, on 0.136 and 0.140 alike: it is
FastAPI's own rendering of every included route with its prefix applied and
every dependency it inherits resolved — a router-level `require_api_key`
included as part of a parent's `dependencies` shows on each operation. It
is also the contract a client generates from, so a guard that does not show
there is one a generated client would not know to satisfy.

  * `require_api_key` is a plain header dependency: the operation carries an
    `X-API-Key` header parameter.
  * The fail-closed guards (`require_adjudicator`, `require_operator_key`,
    `require_seal_key`) use the `OperatorApiKey` security scheme: the
    operation carries a security requirement naming it.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI

WRITE_METHODS = ("post", "put", "patch", "delete")
OPERATOR_SCHEME = "OperatorApiKey"

Guard = Literal["api_key", "operator_key_fail_closed"]


def write_operations(app: FastAPI) -> dict[tuple[str, str], dict[str, Any]]:
    """Every state-changing operation, as (METHOD, path template) -> its OpenAPI operation."""
    spec = app.openapi()
    return {
        (method.upper(), path): operation
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in WRITE_METHODS
    }


def operator_guard(operation: dict[str, Any]) -> Guard | None:
    """Which operator-key guard an operation is behind, if any."""
    if any(OPERATOR_SCHEME in requirement for requirement in operation.get("security", [])):
        return "operator_key_fail_closed"
    headers = {p["name"].lower() for p in operation.get("parameters", []) if p.get("in") == "header"}
    return "api_key" if "x-api-key" in headers else None


def concrete(path: str) -> str:
    """A template path with a plausible value in every parameter, for a request or a policy match."""
    return (
        path.replace("{agent_id}", "agt_01h8")
        .replace("{dispute_id}", "dsp_0000000000000000")
        .replace("{ramp_id}", "ramp_000000000000")
    )
