"""Every input a write route accepts is bounded — read off the published schema.

The body cap (BodyLimitMiddleware, 1 MiB) bounds a request; it does not bound
a FIELD. A 1 MiB agent name, a 100 000-entry list or a float of 1e308 all fit
under it, and each is a different failure further in: a prompt that blows its
budget, a loop that runs for seconds, an i128 conversion that overflows on
chain after the user has already signed. So every field a state-changing
route reads must carry its own bound, and this test reads them off the
OpenAPI document — the same contract a client generates from — so a new
route or field that forgets one fails here rather than in production.

Bounded means: a string has a maxLength, a pattern, or a closed set (enum,
const); an array has maxItems; a number has both a lower and an upper bound.

The audit that wrote this found every public route already bounded; the gaps
were all on the operator-keyed PDAX routes, whose request models carried
about eighty free-text fields with no limit.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app

_WRITE_METHODS = ("post", "put", "patch", "delete")


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    with TestClient(app) as c:
        return c.get("/openapi.json").json()


def _resolve(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        return spec["components"]["schemas"][name]
    return schema


def _unbounded(schema: dict[str, Any], spec: dict[str, Any], where: str, seen: set[str]) -> list[str]:
    ref = schema.get("$ref")
    if ref:
        if ref in seen:
            return []
        seen = seen | {ref}
    schema = _resolve(schema, spec)
    problems: list[str] = []
    for key in ("anyOf", "oneOf", "allOf"):
        for part in schema.get(key, []):
            problems += _unbounded(part, spec, f"{where}", seen)
    kind = schema.get("type")
    if kind == "string" and not ({"maxLength", "pattern", "enum", "const"} & set(schema)):
        problems.append(f"{where}: unbounded string")
    elif kind == "array":
        if "maxItems" not in schema:
            problems.append(f"{where}: unbounded array")
        problems += _unbounded(schema.get("items", {}), spec, f"{where}[]", seen)
    elif kind in ("number", "integer"):
        low = {"minimum", "exclusiveMinimum"} & set(schema)
        high = {"maximum", "exclusiveMaximum"} & set(schema)
        if not (low and high) and "enum" not in schema:
            problems.append(f"{where}: unbounded {kind}")
    elif kind == "object":
        for name, prop in schema.get("properties", {}).items():
            problems += _unbounded(prop, spec, f"{where}.{name}", seen)
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            problems.append(f"{where}: open-ended object")
    return problems


def test_every_write_route_bounds_every_field_it_accepts(spec: dict[str, Any]) -> None:
    problems: list[str] = []
    for path, item in spec["paths"].items():
        for method in _WRITE_METHODS:
            op = item.get(method)
            if op is None:
                continue
            where = f"{method.upper()} {path}"
            for param in op.get("parameters", []):
                # Headers are bounded below the app: uvicorn's HTTP parser
                # refuses an oversized header block before any route runs.
                if param["in"] == "header":
                    continue
                problems += _unbounded(param.get("schema", {}), spec, f"{where} {param['in']}:{param['name']}", set())
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            if body is not None:
                problems += _unbounded(body, spec, f"{where} body", set())

    assert problems == [], "\n".join(problems)


def test_the_published_bound_is_the_enforced_one() -> None:
    # The PDAX models publish their bound through a schema hook; this pins
    # that the validator refuses past the same number, so the two cannot drift.
    from pydantic import ValidationError

    from app.pdax.models.common import REQUEST_STR_MAX_LENGTH
    from app.pdax.models.webhooks import WebhookRegisterRequest

    schema = WebhookRegisterRequest.model_json_schema()
    assert all(p.get("maxLength", 0) <= REQUEST_STR_MAX_LENGTH for p in schema["properties"].values())
    with pytest.raises(ValidationError):
        WebhookRegisterRequest.model_validate(
            {name: "x" * (REQUEST_STR_MAX_LENGTH + 1) for name in schema.get("required", [])}
        )
