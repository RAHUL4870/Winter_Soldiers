"""Common schemas shared across multiple endpoints."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic pagination envelope for list endpoints.

    Wraps any list of items with pagination metadata. Reusable across
    shipments, alerts, audit logs, etc.

    Example:
        PaginatedResponse[ShipmentListItem](
            items=[...],
            total=150,
            page=1,
            size=20,
            total_pages=8
        )
    """

    items: list[T] = Field(..., description="Items for the current page")
    total: int = Field(..., description="Total count of matching records (all pages)")
    page: int = Field(..., description="Current page number (1-indexed)")
    size: int = Field(..., description="Number of items per page")
    total_pages: int = Field(..., description="Total number of pages")
