"""Centralized test harness for NexaFreight Control Tower.

ISOLATION STRATEGY (chosen and documented):
This test suite uses **fresh schema per test** isolation:
- Each test function gets a new in-memory SQLite database
- create_all_tables() runs before the test
- drop_all_tables() runs after the test
- No transaction rollback pattern (simpler, more explicit cleanup)

Tradeoff rationale:
- PRO: Complete isolation — no cross-test contamination possible
- PRO: Simpler mental model — each test sees empty tables
- CON: Slightly slower than transaction rollback (acceptable for test suite <100 tests)
- CON: Alembic migrations not exercised in unit tests (deliberate — integration tests cover that)

This is the FINAL, canonical test harness. All future backend tests (T-021+)
import fixtures from here. Do not create ad-hoc app/db/auth setup in individual
test files.

PRODUCTION DATABASE SAFETY:
All fixtures guarantee tests never touch data/nexafreight.db. Every test uses
in-memory SQLite or explicitly tmp_path-isolated file databases.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from nexafreight.auth import create_access_token, hash_password
from nexafreight.config import Settings, get_settings
from nexafreight.database import (
    create_all_tables,
    create_session_factory,
    create_test_engine,
    dispose_engine,
    drop_all_tables,
    get_db,
    get_db_session,
)
from nexafreight.enums import (
    CargoClass,
    LocationType,
    ShipmentStatus,
    TransportMode,
    UserRole,
)
from nexafreight.main import create_app
from nexafreight.models import Leg, Location, Order, Shipment, User

# ============================================================================
# DATABASE FIXTURES
# ============================================================================


@pytest_asyncio.fixture
async def test_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Provide a clean in-memory SQLite engine for each test.

    Scope: function (fresh schema per test for complete isolation).
    Cleanup: drops all tables and disposes engine after test completes.
    """
    engine = create_test_engine("sqlite+aiosqlite:///:memory:")
    await create_all_tables(engine)
    yield engine
    await drop_all_tables(engine)
    await dispose_engine(engine)


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a clean AsyncSession for each test.

    Each test gets a fresh session bound to the test engine.
    No explicit transaction rollback pattern — schema is dropped/recreated
    per test instead (see isolation strategy docstring above).
    """
    session_factory = create_session_factory(test_engine)
    async with session_factory() as session:
        yield session


# ============================================================================
# APPLICATION FIXTURES
# ============================================================================


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Provide test settings with isolated database and test-safe secrets.

    Uses tmp_path for any file-based database needs (though most tests
    use in-memory via test_engine).
    """
    return Settings(
        jwt_secret="test-jwt-secret-at-least-32-characters-long-for-security",
        jwt_algorithm="HS256",
        jwt_expiry_minutes=60,
        bcrypt_rounds=4,  # Low rounds for fast test execution
        environment="test",
        database_path=tmp_path / "test.db",  # Not used by in-memory tests, but safe
        allowed_origins=["http://localhost:3000", "http://localhost:5173"],
    )


@pytest_asyncio.fixture
async def test_app(
    test_settings: Settings,
    test_engine: AsyncEngine,
) -> AsyncGenerator[FastAPI, None]:
    """Provide FastAPI app with test database dependency override.

    Overrides get_db_session to use the test engine instead of production.
    """
    app = create_app(test_settings)

    # Override database dependency to use test engine
    session_factory = create_session_factory(test_engine)

    async def override_get_db_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_db] = override_get_db_session
    app.dependency_overrides[get_settings] = lambda: test_settings

    yield app

    # Cleanup
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(test_app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """Provide async HTTP client for testing FastAPI endpoints.

    Uses httpx.AsyncClient with ASGI transport for true async testing.
    """
    transport = ASGITransport(app=test_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ============================================================================
# USER/AUTH FIXTURES
# ============================================================================


@pytest_asyncio.fixture
async def seed_admin_user(db_session: AsyncSession, test_settings: Settings) -> User:
    """Create and return an admin user for tests."""
    hashed_pw = hash_password("admin_test_password", test_settings)
    user = User(
        email="admin@example.com",
        hashed_password=hashed_pw,
        full_name="Test Admin",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def seed_operator_user(db_session: AsyncSession, test_settings: Settings) -> User:
    """Create and return an operator user for tests."""
    hashed_pw = hash_password("operator_test_password", test_settings)
    user = User(
        email="operator@example.com",
        hashed_password=hashed_pw,
        full_name="Test Operator",
        role=UserRole.OPERATOR,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def seed_viewer_user(db_session: AsyncSession, test_settings: Settings) -> User:
    """Create and return a viewer user for tests."""
    hashed_pw = hash_password("viewer_test_password", test_settings)
    user = User(
        email="viewer@example.com",
        hashed_password=hashed_pw,
        full_name="Test Viewer",
        role=UserRole.VIEWER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def test_user(seed_operator_user: User) -> User:
    """Compatibility alias for existing tests."""
    return seed_operator_user


@pytest.fixture
def auth_headers_factory(test_settings: Settings) -> Callable[[User], dict[str, str]]:
    """Factory for generating Authorization headers for any user.

    Returns a callable that takes a User and returns auth headers dict.

    Usage:
        headers = auth_headers_factory(admin_user)
        response = await client.get("/protected", headers=headers)
    """

    def _make_headers(user: User) -> dict[str, str]:
        role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
        token = create_access_token(
            user_email=user.email,
            user_role=role_val,
            settings=test_settings,
        )
        return {"Authorization": f"Bearer {token}"}

    return _make_headers


@pytest_asyncio.fixture
async def auth_headers(
    auth_headers_factory: Callable[[User], dict[str, str]],
    seed_operator_user: User,
) -> dict[str, str]:
    """Shortcut fixture returning auth headers for the default operator user."""
    return auth_headers_factory(seed_operator_user)


# ============================================================================
# DOMAIN FACTORY FIXTURES
# ============================================================================


@pytest_asyncio.fixture
async def make_location(db_session: AsyncSession) -> Callable[..., AsyncGenerator[Location, None]]:
    """Factory for creating test Location entities.

    Returns a callable that creates and persists a Location with sensible defaults.

    Usage:
        location = await make_location(locode="USNYC", name="New York")
    """

    async def _make(
        locode: str = "USNYC",
        name: str = "New York",
        country_code: str = "US",
        location_type: LocationType = LocationType.PORT,
        latitude: float = 40.7128,
        longitude: float = -74.0060,
    ) -> Location:
        result = await db_session.execute(select(Location).where(Location.locode == locode))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing
        loc = Location(
            locode=locode,
            name=name,
            country_code=country_code,
            location_type=location_type,
            latitude=latitude,
            longitude=longitude,
        )
        db_session.add(loc)
        await db_session.commit()
        await db_session.refresh(loc)
        return loc

    return _make


@pytest_asyncio.fixture
async def make_shipment(
    db_session: AsyncSession,
    make_location: Callable[..., Location],
) -> Callable[..., Shipment]:
    """Factory for creating test Shipment entities.

    Automatically creates origin/destination locations if not provided.

    Usage:
        shipment = await make_shipment(
            primary_transport_mode=TransportMode.SEA,
            status=ShipmentStatus.IN_TRANSIT
        )
    """

    async def _make(
        origin: Location | None = None,
        destination: Location | None = None,
        primary_transport_mode: TransportMode = TransportMode.SEA,
        cargo_class: CargoClass = CargoClass.STANDARD,
        status: ShipmentStatus = ShipmentStatus.PLANNED,
        container_count: int = 1,
        route_version: int = 1,
    ) -> Shipment:
        if origin is None:
            origin = await make_location(locode="USNYC", name="New York")
        if destination is None:
            destination = await make_location(locode="NLRTM", name="Rotterdam")

        shipment = Shipment(
            origin_id=origin.id,
            destination_id=destination.id,
            primary_transport_mode=primary_transport_mode,
            cargo_class=cargo_class,
            status=status,
            container_count=container_count,
            route_version=route_version,
        )
        db_session.add(shipment)
        await db_session.commit()
        await db_session.refresh(shipment)
        return shipment

    return _make


@pytest_asyncio.fixture
async def make_order(
    db_session: AsyncSession,
    make_shipment: Callable[..., Shipment],
) -> Callable[..., Order]:
    """Factory for creating test Order entities.

    Automatically creates a shipment if not provided.

    Usage:
        order = await make_order(
            order_number="ORD-001",
            revenue=5000.0,
            shipping_cost=500.0
        )
    """

    async def _make(
        order_number: str = "ORD-TEST-001",
        shipment: Shipment | None = None,
        revenue: float = 10000.0,
        shipping_cost: float = 1000.0,
        sla_deadline: datetime | None = None,
        shipping_mode: TransportMode = TransportMode.SEA,
        cargo_class: CargoClass = CargoClass.STANDARD,
    ) -> Order:
        if shipment is None:
            shipment = await make_shipment()
        if sla_deadline is None:
            sla_deadline = datetime.now(UTC) + timedelta(days=30)

        order = Order(
            order_number=order_number,
            shipment_id=shipment.id,
            revenue=revenue,
            shipping_cost=shipping_cost,
            sla_deadline=sla_deadline,
            shipping_mode=shipping_mode,
            cargo_class=cargo_class,
        )
        db_session.add(order)
        await db_session.commit()
        await db_session.refresh(order)
        return order

    return _make


@pytest_asyncio.fixture
async def make_leg(
    db_session: AsyncSession,
    make_location: Callable[..., Location],
) -> Callable[..., Leg]:
    """Factory for creating test Leg entities."""
    from nexafreight.enums import LegStatus, Provenance, TransportMode
    from nexafreight.models import Leg

    async def _make(
        shipment_id: str,
        sequence_number: int = 1,
        route_version: int = 1,
        mode: str | TransportMode = TransportMode.ROAD,
        status: str | LegStatus = LegStatus.PLANNED,
        origin: Location | None = None,
        destination: Location | None = None,
        route_geometry: str | None = None,
        planned_departure: datetime | None = None,
        planned_arrival: datetime | None = None,
        actual_departure: datetime | None = None,
        actual_arrival: datetime | None = None,
        provenance: Provenance = Provenance.SIMULATED,
    ) -> Leg:
        if origin is None:
            origin = await make_location(locode="USNYC", name="New York")
        if destination is None:
            destination = await make_location(locode="USCHI", name="Chicago")

        transport_mode_val = mode if isinstance(mode, TransportMode) else TransportMode(mode)
        status_val = status if isinstance(status, LegStatus) else LegStatus(status)
        now = datetime.now(UTC)
        leg = Leg(
            shipment_id=shipment_id,
            sequence_number=sequence_number,
            route_version=route_version,
            transport_mode=transport_mode_val,
            status=status_val,
            origin_id=origin.id,
            destination_id=destination.id,
            route_geometry_json=route_geometry,
            planned_departure=planned_departure or now,
            planned_arrival=planned_arrival or (now + timedelta(hours=4)),
            actual_departure=actual_departure,
            actual_arrival=actual_arrival,
            provenance=provenance,
        )
        db_session.add(leg)
        await db_session.commit()
        await db_session.refresh(leg)
        return leg

    return _make
