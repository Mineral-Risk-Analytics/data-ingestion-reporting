from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = Field(default="battery-data-intelligence-engine")


class PaginatedResponse(BaseModel, Generic[T]):
    """Standard list-endpoint envelope: {data, total, page, limit}."""

    data: list[T]
    total: int
    page: int
    limit: int


class ErrorResponse(BaseModel):
    error: str
    detail: str | list | dict | None = None
