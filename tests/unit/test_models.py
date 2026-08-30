"""Tests for complete ORM domain model."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError

from nexafreight.database import (
    create_all_tables,
    create_session_factory,
    create_test_engine,
    drop_all_tables,
)
from nexafreight.enums import (
    AlertSeverity,
    AlertStatus,
    CargoClass,
    DecisionAction,
    DisruptionStatus,
    DisruptionType,
    LegStatus,
    LocationType,
    OrderSlaStatus,
    Provenance,
    ShipmentStatus,
    TransportMode,
    UserRole,
)
from nexafreight.models import (
    Alert,
    AuditLog,
    CorridorAlternative,
    Decision,
    Disruption,
    Leg,
    Location,
    Order,
    OrderItem,
    Port,
    PortDailyStat,
    PositionReport,
    Shipment,
    User,
    Vessel,
)
from nexafreight.models.base import Base


@pytest.mark.asyncio
async def test_all_models_registered_in_metadata():
    """Verify all expected tables are registered on Base.metadata."""
    expected_tables = {
        "users",
        "locations",
        "ports",
        "port_daily_stats",
        "vessels",
        "orders",
        "order_items",
        "shipments",
        "legs",
        "position_reports",
        "disruptions",
        "alerts",
        "corridor_alternatives",
        "decisions",
        "audit_logs",
    }
    # Import models to populate metadata
    import nexafreight.models  # noqa: F401

    actual_tables = set(Base.metadata.tables.keys()) - {"probe_test_table"}
    assert expected_tables == actual_tables, (
        f"Missing or extra tables: {expected_tables ^ actual_tables}"
    )


@pytest.mark.asyncio
async def test_schema_can_be_created_without_errors():
    """Verify complete schema can be created from metadata."""
    engine = create_test_engine()
    # Should not raise
    await create_all_tables(engine)

    # Verify tables exist
    import nexafreight.models  # noqa: F401

    async with engine.begin() as conn:
        result = await conn.execute(
            select(1).select_from(User.__table__).limit(0)  # Just check table exists
        )
        assert result is not None

    await drop_all_tables(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_complete_entity_graph_insertion():
    """Verify full entity graph can be inserted in proper dependency order."""
    engine = create_test_engine()
    await create_all_tables(engine)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        # 1. Create user
        user = User(
            email="operator@nexafreight.com",
            hashed_password="hashed_pw_placeholder",
            role=UserRole.OPERATOR,
            is_active=True,
        )
        session.add(user)
        await session.flush()

        # 2. Create locations
        origin = Location(
            locode="USNYC",
            name="New York",
            country_code="US",
            location_type=LocationType.PORT,
            latitude=40.7128,
            longitude=-74.0060,
        )
        destination = Location(
            locode="NLRTM",
            name="Rotterdam",
            country_code="NL",
            location_type=LocationType.PORT,
            latitude=51.9225,
            longitude=4.47917,
        )
        session.add_all([origin, destination])
        await session.flush()

        # 3. Create port
        port = Port(location_id=origin.id)
        session.add(port)
        await session.flush()

        # 4. Create port daily stat
        port_stat = PortDailyStat(
            port_id=port.id,
            stat_date=date.today(),
            congestion_index=0.75,
        )
        session.add(port_stat)

        # 5. Create vessel
        vessel = Vessel(
            mmsi=123456789,
            name="MSC Aurora",
            call_sign="ABCD1",
            typical_lanes_json='["USNYC-NLRTM", "NLRTM-CNSHA"]',
        )
        session.add(vessel)
        await session.flush()

        # 6. Create shipment
        shipment = Shipment(
            origin_id=origin.id,
            destination_id=destination.id,
            primary_transport_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
            container_count=2,
            status=ShipmentStatus.PLANNED,
            route_version=1,
        )
        session.add(shipment)
        await session.flush()

        # 7. Create order
        order = Order(
            order_number="ORD-2024-001",
            shipment_id=shipment.id,
            sla_deadline=datetime.now(UTC) + timedelta(days=30),
            revenue=15000.0,
            shipping_cost=2500.0,
            sla_status=OrderSlaStatus.ON_TIME,
            shipping_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
            historical_late_delivery=False,
        )
        session.add(order)
        await session.flush()

        # 8. Create order item
        order_item = OrderItem(
            order_id=order.id,
            product_category="Electronics",
            quantity=100,
            unit_price=150.0,
        )
        session.add(order_item)

        # 9. Create leg
        geo_json = '{"type":"LineString","coordinates":[[-74.0060,40.7128],[4.47917,51.9225]]}'
        leg = Leg(
            shipment_id=shipment.id,
            sequence_number=1,
            route_version=1,
            transport_mode=TransportMode.SEA,
            status=LegStatus.PLANNED,
            origin_id=origin.id,
            destination_id=destination.id,
            vessel_id=vessel.id,
            planned_departure=datetime.now(UTC) + timedelta(days=1),
            planned_arrival=datetime.now(UTC) + timedelta(days=15),
            route_geometry_json=geo_json,
            distance_km=5850.0,
            co2_kg=1200.0,
            provenance=Provenance.REAL,
        )
        session.add(leg)
        await session.flush()

        # 10. Create position report
        position = PositionReport(
            leg_id=leg.id,
            asset_type="vessel",
            mmsi=vessel.mmsi,
            latitude=40.5,
            longitude=-73.5,
            heading=90.0,
            speed_knots=18.5,
            reported_at=datetime.now(UTC),
            provenance=Provenance.REAL,
        )
        session.add(position)

        # 11. Create disruption
        disruption = Disruption(
            shipment_id=shipment.id,
            leg_id=leg.id,
            disruption_type=DisruptionType.VESSEL_DELAY,
            status=DisruptionStatus.ACTIVE,
            description="Vessel delayed due to port congestion",
            detected_at=datetime.now(UTC),
        )
        session.add(disruption)
        await session.flush()

        # 12. Create alert
        alert = Alert(
            disruption_id=disruption.id,
            shipment_id=shipment.id,
            severity=AlertSeverity.HIGH,
            status=AlertStatus.OPEN,
            financial_exposure=3000.0,
            sla_breach_details_json='{"ORD-2024-001": {"delay_hours": 48}}',
        )
        session.add(alert)
        await session.flush()

        # 13. Create corridor alternative
        corridor = CorridorAlternative(
            option_key="USNYC-NLRTM-AIR",
            display_name="Air freight via FRA",
            applicable_disruption_types_json='["VESSEL_DELAY", "PORT_CONGESTION"]',
            route_template_json='{"mode": "AIR", "hub": "DEFRA"}',
            cost_delta_factor=2.5,
            time_delta_hours=-120.0,
            co2_delta_factor=3.0,
        )
        session.add(corridor)

        # 14. Create decision
        decision = Decision(
            alert_id=alert.id,
            shipment_id=shipment.id,
            action=DecisionAction.ACCEPT_DELAY,
            options_snapshot_json='[{"key": "ACCEPT", "impact": 3000}]',
            financial_impact=3000.0,
            route_version_before=1,
            route_version_after=1,
            approved_by=user.id,
        )
        session.add(decision)

        # 15. Create audit log
        audit = AuditLog(
            actor_type="user",
            actor_id=user.id,
            actor_name="operator@nexafreight.com",
            action="approve_decision",
            entity_type="decision",
            entity_id=decision.id,
            details_json='{"action": "ACCEPT_DELAY"}',
        )
        session.add(audit)

        # Commit all
        await session.commit()

        # Verify entities were created
        result = await session.execute(select(Shipment).where(Shipment.id == shipment.id))
        retrieved_shipment = result.scalar_one()
        assert retrieved_shipment.primary_transport_mode == TransportMode.SEA

        result = await session.execute(select(Order).where(Order.id == order.id))
        retrieved_order = result.scalar_one()
        assert retrieved_order.order_number == "ORD-2024-001"

        result = await session.execute(select(Alert).where(Alert.id == alert.id))
        retrieved_alert = result.scalar_one()
        assert retrieved_alert.severity == AlertSeverity.HIGH

    await drop_all_tables(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_foreign_key_enforcement():
    """Foreign key constraints are enforced."""
    engine = create_test_engine()
    await create_all_tables(engine)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        # Attempt to create leg with non-existent shipment
        leg = Leg(
            shipment_id="non-existent-uuid",
            sequence_number=1,
            route_version=1,
            transport_mode=TransportMode.SEA,
            status=LegStatus.PLANNED,
            origin_id=999999,  # Also doesn't exist
            destination_id=999998,
            planned_departure=datetime.now(UTC),
            planned_arrival=datetime.now(UTC) + timedelta(days=1),
            provenance=Provenance.MOCK,
        )
        session.add(leg)
        with pytest.raises(IntegrityError):
            await session.commit()

    await drop_all_tables(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_unique_constraint_duplicate_active_disruption():
    """Duplicate active disruptions for same shipment/leg/type are rejected."""
    engine = create_test_engine()
    await create_all_tables(engine)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        # Create minimal dependencies
        origin = Location(
            locode="USNYC",
            name="New York",
            country_code="US",
            location_type=LocationType.PORT,
            latitude=40.7128,
            longitude=-74.0060,
        )
        session.add(origin)
        await session.flush()

        shipment = Shipment(
            origin_id=origin.id,
            destination_id=origin.id,
            primary_transport_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
        )
        session.add(shipment)
        await session.flush()

        leg = Leg(
            shipment_id=shipment.id,
            sequence_number=1,
            route_version=1,
            transport_mode=TransportMode.SEA,
            origin_id=origin.id,
            destination_id=origin.id,
            planned_departure=datetime.now(UTC),
            planned_arrival=datetime.now(UTC) + timedelta(days=1),
            provenance=Provenance.MOCK,
        )
        session.add(leg)
        await session.flush()

        # Create first disruption
        disruption1 = Disruption(
            shipment_id=shipment.id,
            leg_id=leg.id,
            disruption_type=DisruptionType.VESSEL_DELAY,
            status=DisruptionStatus.ACTIVE,
            description="First delay",
            detected_at=datetime.now(UTC),
        )
        session.add(disruption1)
        await session.commit()

    # Attempt duplicate in new session
    async with session_factory() as session:
        disruption2 = Disruption(
            shipment_id=shipment.id,
            leg_id=leg.id,
            disruption_type=DisruptionType.VESSEL_DELAY,
            status=DisruptionStatus.ACTIVE,
            description="Duplicate delay",
            detected_at=datetime.now(UTC),
        )
        session.add(disruption2)
        match_pattern = r"(uq_disruptions_active_per_shipment_leg_type|UNIQUE constraint failed)"
        with pytest.raises(IntegrityError, match=match_pattern):
            await session.commit()

    await drop_all_tables(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_unique_constraint_one_alert_per_disruption():
    """Only one alert can exist per disruption."""
    engine = create_test_engine()
    await create_all_tables(engine)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        # Create minimal dependencies
        origin = Location(
            locode="USNYC",
            name="New York",
            country_code="US",
            location_type=LocationType.PORT,
            latitude=40.7128,
            longitude=-74.0060,
        )
        session.add(origin)
        await session.flush()

        shipment = Shipment(
            origin_id=origin.id,
            destination_id=origin.id,
            primary_transport_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
        )
        session.add(shipment)
        await session.flush()

        disruption = Disruption(
            shipment_id=shipment.id,
            disruption_type=DisruptionType.PORT_CONGESTION,
            status=DisruptionStatus.ACTIVE,
            description="Congestion",
            detected_at=datetime.now(UTC),
        )
        session.add(disruption)
        await session.flush()

        # Create first alert
        alert1 = Alert(
            disruption_id=disruption.id,
            shipment_id=shipment.id,
            severity=AlertSeverity.HIGH,
            financial_exposure=5000.0,
        )
        session.add(alert1)
        await session.commit()

    # Attempt duplicate alert
    async with session_factory() as session:
        alert2 = Alert(
            disruption_id=disruption.id,
            shipment_id=shipment.id,
            severity=AlertSeverity.MEDIUM,
            financial_exposure=3000.0,
        )
        session.add(alert2)
        match_pattern = r"(uq_alerts_disruption_id|UNIQUE constraint failed)"
        with pytest.raises(IntegrityError, match=match_pattern):
            await session.commit()

    await drop_all_tables(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_unique_constraint_one_decision_per_alert():
    """Only one decision can exist per alert."""
    engine = create_test_engine()
    await create_all_tables(engine)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        # Create minimal dependencies
        user = User(
            email="op@example.com",
            hashed_password="hash",
            role=UserRole.OPERATOR,
        )
        origin = Location(
            locode="USNYC",
            name="New York",
            country_code="US",
            location_type=LocationType.PORT,
            latitude=40.7128,
            longitude=-74.0060,
        )
        session.add_all([user, origin])
        await session.flush()

        shipment = Shipment(
            origin_id=origin.id,
            destination_id=origin.id,
            primary_transport_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
        )
        session.add(shipment)
        await session.flush()

        disruption = Disruption(
            shipment_id=shipment.id,
            disruption_type=DisruptionType.WEATHER,
            status=DisruptionStatus.ACTIVE,
            description="Storm",
            detected_at=datetime.now(UTC),
        )
        session.add(disruption)
        await session.flush()

        alert = Alert(
            disruption_id=disruption.id,
            shipment_id=shipment.id,
            severity=AlertSeverity.CRITICAL,
            financial_exposure=10000.0,
        )
        session.add(alert)
        await session.flush()

        # Create first decision
        decision1 = Decision(
            alert_id=alert.id,
            shipment_id=shipment.id,
            action=DecisionAction.REROUTE,
            options_snapshot_json="[]",
            financial_impact=8000.0,
            route_version_before=1,
            route_version_after=2,
            approved_by=user.id,
        )
        session.add(decision1)
        await session.commit()

    # Attempt duplicate decision
    async with session_factory() as session:
        decision2 = Decision(
            alert_id=alert.id,
            shipment_id=shipment.id,
            action=DecisionAction.ACCEPT_DELAY,
            options_snapshot_json="[]",
            financial_impact=10000.0,
            route_version_before=1,
            route_version_after=1,
            approved_by=user.id,
        )
        session.add(decision2)
        match_pattern = r"(uq_decisions_alert_id|UNIQUE constraint failed)"
        with pytest.raises(IntegrityError, match=match_pattern):
            await session.commit()

    await drop_all_tables(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_unique_constraint_duplicate_port_daily_stat():
    """Duplicate port stats for same port+date are rejected."""
    engine = create_test_engine()
    await create_all_tables(engine)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        location = Location(
            locode="USNYC",
            name="New York",
            country_code="US",
            location_type=LocationType.PORT,
            latitude=40.7128,
            longitude=-74.0060,
        )
        session.add(location)
        await session.flush()

        port = Port(location_id=location.id)
        session.add(port)
        await session.flush()

        stat1 = PortDailyStat(
            port_id=port.id,
            stat_date=date(2024, 1, 15),
            congestion_index=0.8,
        )
        session.add(stat1)
        await session.commit()

    # Attempt duplicate
    async with session_factory() as session:
        stat2 = PortDailyStat(
            port_id=port.id,
            stat_date=date(2024, 1, 15),
            congestion_index=0.9,
        )
        session.add(stat2)
        match_pattern = r"(uq_port_daily_stats_port_id_stat_date|UNIQUE constraint failed)"
        with pytest.raises(IntegrityError, match=match_pattern):
            await session.commit()

    await drop_all_tables(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_values_behave_correctly():
    """Default values are applied as expected."""
    engine = create_test_engine()
    await create_all_tables(engine)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        origin = Location(
            locode="USNYC",
            name="New York",
            country_code="US",
            location_type=LocationType.PORT,
            latitude=40.7128,
            longitude=-74.0060,
        )
        session.add(origin)
        await session.flush()

        # Shipment without explicit status or route_version
        shipment = Shipment(
            origin_id=origin.id,
            destination_id=origin.id,
            primary_transport_mode=TransportMode.SEA,
            cargo_class=CargoClass.STANDARD,
        )
        session.add(shipment)
        await session.commit()

        # Verify defaults
        assert shipment.status == ShipmentStatus.PLANNED
        assert shipment.route_version == 1
        assert shipment.container_count == 1

    await drop_all_tables(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_enum_value_rejected():
    """Invalid enum values are rejected by SQLAlchemy/database."""
    engine = create_test_engine()
    await create_all_tables(engine)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        user = User(
            email="test@example.com",
            hashed_password="hash",
            role="INVALID_ROLE",  # type: ignore # Intentionally invalid
        )
        session.add(user)
        # SQLAlchemy will raise error during flush/commit for invalid enum
        with pytest.raises((ValueError, IntegrityError, StatementError, LookupError)):
            await session.commit()

    await drop_all_tables(engine)
    await engine.dispose()
