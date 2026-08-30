"""Unit tests for order-to-shipment consolidation engine (T-017).

Tests the real src/nexafreight/services/consolidation.py implementation
using in-memory SQLite and fixture-created Order rows only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.enums import CargoClass, OrderSlaStatus, TransportMode
from nexafreight.models import Order, Shipment
from nexafreight.services.consolidation import consolidate_orders


def _week_of(dt: datetime) -> int:
    """ISO calendar week for a datetime (matches implementation's grouping)."""
    return dt.isocalendar()[1]


@pytest.mark.asyncio
async def test_correct_grouping_key(db_session: AsyncSession) -> None:
    """Orders sharing origin/dest/mode/cargo/week group into one shipment."""
    # Two orders, identical dimensions, same week
    base_time = datetime(2024, 1, 3, tzinfo=UTC)  # Wednesday, week 1
    o1 = Order(
        order_number="ORD-G1-A",
        sla_deadline=base_time + timedelta(days=20),
        revenue=1000.0,
        shipping_cost=100.0,
        sla_status=OrderSlaStatus.ON_TIME,
        shipping_mode=TransportMode.SEA,
        cargo_class=CargoClass.STANDARD,
        origin_id=1,
        destination_id=2,
    )
    o2 = Order(
        order_number="ORD-G1-B",
        sla_deadline=base_time + timedelta(days=25),
        revenue=2000.0,
        shipping_cost=200.0,
        sla_status=OrderSlaStatus.ON_TIME,
        shipping_mode=TransportMode.SEA,
        cargo_class=CargoClass.STANDARD,
        origin_id=1,
        destination_id=2,
    )
    # Different dimension (different cargo_class) -> must NOT join above group
    o3 = Order(
        order_number="ORD-OTHER",
        sla_deadline=base_time + timedelta(days=22),
        revenue=1500.0,
        shipping_cost=150.0,
        sla_status=OrderSlaStatus.ON_TIME,
        shipping_mode=TransportMode.SEA,
        cargo_class=CargoClass.REFRIGERATED,  # differs
        origin_id=1,
        destination_id=2,
    )
    db_session.add_all([o1, o2, o3])
    await db_session.commit()

    shipments = await consolidate_orders(db_session)

    # Should create exactly 2 shipments: one for (STANDARD group), one for REFRIGERATED
    assert len(shipments) == 2

    # Verify grouping: find shipment containing o1 and o2
    result = await db_session.execute(select(Shipment))
    shipments_in_db = result.scalars().all()
    assert len(shipments_in_db) == 2

    # Reload orders to get updated shipment_id
    await db_session.refresh(o1)
    await db_session.refresh(o2)
    await db_session.refresh(o3)

    assert o1.shipment_id is not None
    assert o1.shipment_id == o2.shipment_id  # grouped together
    assert o3.shipment_id != o1.shipment_id  # different group


@pytest.mark.asyncio
async def test_week_boundary_correctness(db_session: AsyncSession) -> None:
    """Orders in different ISO weeks are NOT grouped together."""
    # Same origin/dest/mode/cargo, but different weeks
    week1_time = datetime(2024, 1, 3, tzinfo=UTC)  # Week 1
    week2_time = datetime(2024, 1, 10, tzinfo=UTC)  # Week 2

    o1 = Order(
        order_number="ORD-W1",
        sla_deadline=week1_time + timedelta(days=20),
        revenue=1000.0,
        shipping_cost=100.0,
        sla_status=OrderSlaStatus.ON_TIME,
        shipping_mode=TransportMode.AIR,
        cargo_class=CargoClass.STANDARD,
        origin_id=1,
        destination_id=2,
        created_at=week1_time,
    )
    o2 = Order(
        order_number="ORD-W2",
        sla_deadline=week2_time + timedelta(days=20),
        revenue=1000.0,
        shipping_cost=100.0,
        sla_status=OrderSlaStatus.ON_TIME,
        shipping_mode=TransportMode.AIR,
        cargo_class=CargoClass.STANDARD,
        origin_id=1,
        destination_id=2,
        created_at=week2_time,
    )
    db_session.add_all([o1, o2])
    await db_session.commit()

    await consolidate_orders(db_session)

    await db_session.refresh(o1)
    await db_session.refresh(o2)

    assert o1.shipment_id != o2.shipment_id, "Different weeks must not group"


@pytest.mark.asyncio
async def test_strictest_sla_computation(db_session: AsyncSession) -> None:
    """Shipment's strictest_sla_deadline = earliest deadline among grouped orders."""
    base_time = datetime(2024, 1, 3, tzinfo=UTC)
    deadlines = [
        base_time + timedelta(days=30),  # latest
        base_time + timedelta(days=10),  # EARLIEST (strictest)
        base_time + timedelta(days=20),
    ]
    orders = []
    for i, deadline in enumerate(deadlines):
        o = Order(
            order_number=f"ORD-SLA-{i}",
            sla_deadline=deadline,
            revenue=1000.0,
            shipping_cost=100.0,
            sla_status=OrderSlaStatus.ON_TIME,
            shipping_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
            origin_id=1,
            destination_id=2,
            created_at=base_time,
        )
        orders.append(o)
        db_session.add(o)
    await db_session.commit()

    shipments = await consolidate_orders(db_session)

    assert len(shipments) == 1
    shipment = shipments[0]
    assert shipment.strictest_sla_deadline == deadlines[1], "Must be earliest deadline"


@pytest.mark.asyncio
async def test_capacity_limit_splits_shipments(db_session: AsyncSession) -> None:
    """Exceeding container capacity splits into multiple shipments."""
    base_time = datetime(2024, 1, 3, tzinfo=UTC)
    # Create 25 orders (implementation allows max 20 containers, each order=1 container)
    for i in range(25):
        o = Order(
            order_number=f"ORD-CAP-{i}",
            sla_deadline=base_time + timedelta(days=30),
            revenue=1000.0,
            shipping_cost=100.0,
            sla_status=OrderSlaStatus.ON_TIME,
            shipping_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
            origin_id=1,
            destination_id=2,
            created_at=base_time,
        )
        db_session.add(o)
    await db_session.commit()

    shipments = await consolidate_orders(db_session)

    # 25 orders / 20 per shipment = 2 shipments (20 + 5)
    assert len(shipments) == 2
    counts = [s.container_count for s in shipments]
    assert sum(counts) == 25, "Total containers across shipments must be 25"


@pytest.mark.asyncio
async def test_container_count_correctness(db_session: AsyncSession) -> None:
    """Container count on shipment matches number of grouped orders."""
    base_time = datetime(2024, 1, 3, tzinfo=UTC)
    for i in range(5):
        o = Order(
            order_number=f"ORD-CT-{i}",
            sla_deadline=base_time + timedelta(days=30),
            revenue=1000.0,
            shipping_cost=100.0,
            sla_status=OrderSlaStatus.ON_TIME,
            shipping_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
            origin_id=1,
            destination_id=2,
            created_at=base_time,
        )
        db_session.add(o)
    await db_session.commit()

    shipments = await consolidate_orders(db_session)

    assert len(shipments) == 1
    assert shipments[0].container_count == 5


@pytest.mark.asyncio
async def test_single_order_group(db_session: AsyncSession) -> None:
    """A single order still produces a valid shipment."""
    base_time = datetime(2024, 1, 3, tzinfo=UTC)
    o = Order(
        order_number="ORD-SINGLE",
        sla_deadline=base_time + timedelta(days=30),
        revenue=5000.0,
        shipping_cost=500.0,
        sla_status=OrderSlaStatus.ON_TIME,
        shipping_mode=TransportMode.ROAD,
        cargo_class=CargoClass.STANDARD,
        origin_id=1,
        destination_id=2,
        created_at=base_time,
    )
    db_session.add(o)
    await db_session.commit()

    shipments = await consolidate_orders(db_session)

    assert len(shipments) == 1
    assert shipments[0].container_count == 1
    await db_session.refresh(o)
    assert o.shipment_id == shipments[0].id


@pytest.mark.asyncio
async def test_order_to_shipment_linkage(db_session: AsyncSession) -> None:
    """Every consolidated Order.shipment_id points to the resulting Shipment."""
    base_time = datetime(2024, 1, 3, tzinfo=UTC)
    orders = []
    for i in range(3):
        o = Order(
            order_number=f"ORD-LNK-{i}",
            sla_deadline=base_time + timedelta(days=30),
            revenue=1000.0,
            shipping_cost=100.0,
            sla_status=OrderSlaStatus.ON_TIME,
            shipping_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
            origin_id=1,
            destination_id=2,
            created_at=base_time,
        )
        orders.append(o)
        db_session.add(o)
    await db_session.commit()

    shipments = await consolidate_orders(db_session)

    assert len(shipments) == 1
    shipment = shipments[0]
    for o in orders:
        await db_session.refresh(o)
        assert o.shipment_id == shipment.id, f"Order {o.order_number} not linked"


@pytest.mark.asyncio
async def test_empty_input(db_session: AsyncSession) -> None:
    """Consolidating zero orders produces zero shipments, no error."""
    shipments = await consolidate_orders(db_session)
    assert shipments == []


@pytest.mark.asyncio
async def test_idempotency_skipped(db_session: AsyncSession) -> None:
    """Idempotency not applicable — consolidation is a one-time batch process.

    Re-running consolidation on already-consolidated orders would create
    duplicate shipments (Orders already have shipment_id set). This test
    documents that behavior rather than asserting incorrect idempotency.
    """
    base_time = datetime(2024, 1, 3, tzinfo=UTC)
    o = Order(
        order_number="ORD-IDEM",
        sla_deadline=base_time + timedelta(days=30),
        revenue=1000.0,
        shipping_cost=100.0,
        sla_status=OrderSlaStatus.ON_TIME,
        shipping_mode=TransportMode.SEA,
        cargo_class=CargoClass.STANDARD,
        origin_id=1,
        destination_id=2,
        created_at=base_time,
    )
    db_session.add(o)
    await db_session.commit()

    # First run
    shipments_1 = await consolidate_orders(db_session)
    assert len(shipments_1) == 1

    # Second run: order already has shipment_id, so it is skipped by the
    # implementation (WHERE shipment_id IS NULL). No new shipment created.
    shipments_2 = await consolidate_orders(db_session)
    assert len(shipments_2) == 0, "Already-consolidated orders must not create new shipments"
