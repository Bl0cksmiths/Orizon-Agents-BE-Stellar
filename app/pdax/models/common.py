"""
Shared PDAX models — response envelope, pagination, and common literals.

Most PDAX endpoints wrap their payload in `{ "data": ..., "status": "success" }`.
`Envelope` captures that shape; domain models describe the inner `data`.
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")

Side = Literal["buy", "sell"]
TradeStatus = Literal["successful", "failed", "IN PROGRESS", "SUCCESSFUL", "FAILED"]
TxStatus = Literal["pending", "completed", "failed", "PENDING", "COMPLETED", "FAILED"]
AssetType = Literal["FIAT", "CRYPTO", "fiat", "crypto"]


# The longest free-text value any PDAX request field takes: comfortably above a
# real address line, name or reference, far below what the body cap admits.
REQUEST_STR_MAX_LENGTH = 512


def _publish_string_bound(schema: dict[str, Any]) -> None:
    """Write the model-wide string bound into the published schema.

    Pydantic enforces `str_max_length` but leaves it out of the JSON schema, so
    without this a generated client — and tests/test_input_bounds.py, which
    reads the schema — would see every such field as unbounded.
    """
    for prop in schema.get("properties", {}).values():
        for variant in [prop, *prop.get("anyOf", [])]:
            if variant.get("type") == "string" and not {"maxLength", "pattern", "enum", "const"} & set(variant):
                variant["maxLength"] = REQUEST_STR_MAX_LENGTH


class BoundedRequest(BaseModel):
    """Base for every PDAX request body: no string field may exceed REQUEST_STR_MAX_LENGTH.

    A field that declares a tighter `max_length` keeps it. The body cap bounds
    a request, not a field, and these values go on to PDAX's API, our ramp
    store and our logs.
    """

    model_config = ConfigDict(str_max_length=REQUEST_STR_MAX_LENGTH, json_schema_extra=_publish_string_bound)


class Envelope(BaseModel, Generic[T]):
    """The standard `{ data, status }` PDAX response wrapper."""

    data: T
    status: str = "success"


class Pagination(BaseModel):
    """Common pagination query for list endpoints."""

    page: int = 1
    page_size: int = 10
