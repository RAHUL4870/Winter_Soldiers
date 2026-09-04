"""Customer order with SLA and financial terms (financial layer)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexafreight.enums import CargoClass, OrderSlaStatus, TransportMode
from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from nexafreight.models.shipment import Shipment


class Order(Base, TimestampMixin):
    """Customer order with financial terms and SLA deadline.

    Physical/financial separation: Order owns SLA deadline, revenue, and shipping cost;
    Shipment owns the physical journey.
    Uses integer autoincrement PK (high-volume internal reference, not exposed in API).
    shipment_id is nullable because orders are ingested before consolidation.
    """

    __tablename__ = "orders"
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)

    # Nullable FK to Shipment (set later during consolidation)
    shipment_id: Mapped[str | None] = mapped_column(
        ForeignKey("shipments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Financial and SLA terms
    order_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        default=None,
        comment="Original order placement date",
    )
    sla_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revenue: Mapped[float] = mapped_column(
        Float, nullable=False, comment="Total order revenue (USD)"
    )
    shipping_cost: Mapped[float] = mapped_column(
        Float, nullable=False, comment="Shipping cost (USD)"
    )
    sla_status: Mapped[OrderSlaStatus] = mapped_column(
        String(20), nullable=False, default=OrderSlaStatus.ON_TIME
    )

    # Logistics metadata
    shipping_mode: Mapped[TransportMode] = mapped_column(String(20), nullable=False)
    cargo_class: Mapped[CargoClass] = mapped_column(String(20), nullable=False)

    # Historical labels and transit days from source dataset (for ML training)
    historical_late_delivery: Mapped[bool | None] = mapped_column(nullable=True)
    real_shipping_days: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Optional routing hints for consolidation
    origin_id: int | None = None
    destination_id: int | None = None

    def __init__(self, **kwargs: Any) -> None:
        origin_id = kwargs.pop("origin_id", None)
        dest_id = kwargs.pop("destination_id", None)
        super().__init__(**kwargs)
        if origin_id is not None:
            self.origin_id = int(origin_id)
        if dest_id is not None:
            self.destination_id = int(dest_id)

    # Relationships
    shipment: Mapped[Shipment | None] = relationship("Shipment", back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
    )


class OrderItem(Base):
    """Line item within an order.

    No timestamp mixin (inherit created_at from parent Order if needed).
    Uses integer autoincrement PK (internal reference only).
    """

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_category: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False, comment="Price per unit (USD)")

    # Relationships
    order: Mapped[Order] = relationship("Order", back_populates="items")
