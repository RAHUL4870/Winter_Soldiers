#!/usr/bin/env python3
"""
05_plan_routes.py
=================
T-018 (Phase 1, Data Engineer) — Builds all shipment legs with pre-computed GeoJSON geometry.

DEPENDS ON: scripts/04_consolidate_shipments.py (T-017) & scripts/02_ingest_unlocode.py (T-014).

Run order contract:
    1.  scripts/02_ingest_unlocode.py
    2.  scripts/03_ingest_port_data.py
    3.  scripts/01_ingest_dataco.py
    4.  scripts/04_consolidate_shipments.py
    5.  scripts/05_plan_routes.py   <-- YOU ARE HERE
    ...

What it does
------------
1. Loads all shipments and their origin/destination locations from the database.
2. Builds the multi-modal leg sequence by mode:
       SEA:  [FIRST_MILE_ROAD, ORIGIN_HANDLING, SEA_MAIN, DEST_HANDLING, LAST_MILE_ROAD]
       AIR:  [FIRST_MILE_ROAD, AIR_MAIN,        LAST_MILE_ROAD]
       ROAD: [ROAD_MAIN]
       RAIL: [FIRST_MILE_ROAD, RAIL_MAIN,       LAST_MILE_ROAD]
3. Computes exact route geometries:
       - SEA: searoute-py (pinned 1.4.3) with great-circle fallback
       - ROAD: OpenRouteService / circuity-scaled route
       - AIR: Geodesic arc
4. Chains planned departure and arrival timestamps across legs.
5. Computes GLEC-standard CO2 emissions for every leg.
6. Bulk-inserts / upserts all legs into the `legs` table.

Usage
-----
    python scripts/05_plan_routes.py
    python scripts/05_plan_routes.py --limit 50
    python scripts/05_plan_routes.py --limit 50 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    event,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import create_async_engine

# Ensure src/ is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from nexafreight.adapters.routing.road_route import RoadRouter  # noqa: E402
from nexafreight.services.route_planner import (  # noqa: E402
    LocationRef,
    RoutePlan,
    RoutePlanner,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nexafreight.plan_routes")

PROVENANCE = "DERIVED"


# --------------------------------------------------------------------------- #
# Database configuration
# --------------------------------------------------------------------------- #
def get_db_url() -> str:
    try:
        from nexafreight.config import get_settings  # type: ignore

        settings = get_settings()
        return settings.database_url
    except Exception:
        db_path = os.getenv("DATABASE_PATH", "./data/nexafreight.db")
        if (
            not db_path.startswith("./")
            and not db_path.startswith("/")
            and not db_path.startswith(":")
        ):
            db_path = f"./{db_path}"
        return f"sqlite+aiosqlite:///{db_path}"


def _resolve_tables():
    try:
        from nexafreight.models.leg import Leg  # type: ignore
        from nexafreight.models.location import Location  # type: ignore
        from nexafreight.models.shipment import Shipment  # type: ignore

        return Location.__table__, Shipment.__table__, Leg.__table__
    except Exception:
        meta = MetaData()
        loc_tbl = Table(
            "locations",
            meta,
            Column("id", Integer, primary_key=True),
            Column("locode", String(10)),
            Column("latitude", Float),
            Column("longitude", Float),
        )
        shipments_tbl = Table(
            "shipments",
            meta,
            Column("id", String(36), primary_key=True),
            Column("origin_id", Integer, ForeignKey("locations.id")),
            Column("destination_id", Integer, ForeignKey("locations.id")),
            Column("primary_transport_mode", String(20)),
            Column("cargo_class", String(20)),
            Column("container_count", Integer),
            Column("status", String(20)),
            Column("route_version", Integer),
            Column("strictest_sla_deadline", DateTime(timezone=True)),
            Column("created_at", DateTime(timezone=True)),
            Column("updated_at", DateTime(timezone=True)),
        )
        legs_tbl = Table(
            "legs",
            meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column(
                "shipment_id",
                String(36),
                ForeignKey("shipments.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            Column("sequence_number", Integer, nullable=False),
            Column("route_version", Integer, nullable=False),
            Column("transport_mode", String(20), nullable=False),
            Column("status", String(20), nullable=False, default="PLANNED"),
            Column(
                "origin_id",
                Integer,
                ForeignKey("locations.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            Column(
                "destination_id",
                Integer,
                ForeignKey("locations.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            Column("vessel_id", Integer, nullable=True),
            Column("flight_number", String(20), nullable=True),
            Column("planned_departure", DateTime(timezone=True), nullable=False),
            Column("planned_arrival", DateTime(timezone=True), nullable=False),
            Column("actual_departure", DateTime(timezone=True), nullable=True),
            Column("actual_arrival", DateTime(timezone=True), nullable=True),
            Column("route_geometry_json", Text, nullable=True),
            Column("distance_km", Float, nullable=True),
            Column("co2_kg", Float, nullable=True),
            Column("provenance", String(20), nullable=False, default="DERIVED"),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        return loc_tbl, shipments_tbl, legs_tbl


def _set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=30000;")
    finally:
        cursor.close()


# --------------------------------------------------------------------------- #
# Database Loaders & Persisters
# --------------------------------------------------------------------------- #
async def load_locations_map(conn, loc_tbl: Table) -> dict[int, LocationRef]:
    res = await conn.execute(
        select(loc_tbl.c["id"], loc_tbl.c["locode"], loc_tbl.c["latitude"], loc_tbl.c["longitude"])
    )
    rows = res.fetchall()
    loc_map: dict[int, LocationRef] = {}
    for lid, locode, lat, lon in rows:
        if lat is not None and lon is not None:
            loc_map[lid] = LocationRef(
                id=lid, locode=str(locode or ""), lat=float(lat), lon=float(lon)
            )
    return loc_map


async def load_shipments_to_plan(
    conn, shipments_tbl: Table, limit: int = 0
) -> list[dict[str, Any]]:
    stmt = select(
        shipments_tbl.c["id"],
        shipments_tbl.c["origin_id"],
        shipments_tbl.c["destination_id"],
        shipments_tbl.c["primary_transport_mode"],
        shipments_tbl.c["cargo_class"],
        shipments_tbl.c["container_count"],
        shipments_tbl.c["planned_departure"],
        shipments_tbl.c["strictest_sla_deadline"],
        shipments_tbl.c["created_at"],
    )
    if limit > 0:
        stmt = stmt.limit(limit)

    res = await conn.execute(stmt)
    rows = res.fetchall()
    shipment_list = []
    for sid, orig_id, dest_id, mode, cargo, containers, dep, deadline, created in rows:
        shipment_list.append(
            {
                "id": str(sid),
                "origin_id": orig_id,
                "destination_id": dest_id,
                "primary_mode": mode or "SEA",
                "cargo_class": cargo or "STANDARD",
                "container_count": containers or 1,
                "planned_departure": dep or deadline or created or datetime.now(UTC),
                "strictest_sla_deadline": deadline,
                "created_at": created or datetime.now(UTC),
            }
        )
    return shipment_list


async def persist_plans(
    plans: list[RoutePlan],
    legs_tbl: Table,
    batch_size: int = 1000,
) -> None:
    db_url = get_db_url()
    log.info("Connecting to database: %s", db_url)
    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 60},
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(legs_tbl.create, checkfirst=True)

            # Flatten all legs across plans
            all_leg_values = []
            now = datetime.now(UTC)

            for plan in plans:
                for leg in plan.legs:
                    all_leg_values.append(
                        {
                            "shipment_id": plan.shipment_id,
                            "sequence_number": leg.sequence_number,
                            "route_version": leg.route_version,
                            "transport_mode": leg.transport_mode,
                            "status": "PLANNED",
                            "origin_id": leg.origin_id,
                            "destination_id": leg.destination_id,
                            "vessel_id": None,
                            "flight_number": None,
                            "planned_departure": leg.planned_departure,
                            "planned_arrival": leg.planned_arrival,
                            "actual_departure": leg.planned_departure,
                            "actual_arrival": leg.planned_arrival,
                            "route_geometry_json": leg.route_geometry_json,
                            "distance_km": leg.distance_km,
                            "co2_kg": leg.co2_kg,
                            "provenance": leg.provenance,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )

            log.info("Inserting %d legs into 'legs' table...", len(all_leg_values))

            for i in range(0, len(all_leg_values), batch_size):
                chunk = all_leg_values[i : i + batch_size]
                await conn.execute(sqlite_insert(legs_tbl).values(chunk))
                if (i // batch_size) % 10 == 0 or i + batch_size >= len(all_leg_values):
                    log.info(
                        "Inserted legs [%d..%d] of %d",
                        i + 1,
                        min(i + batch_size, len(all_leg_values)),
                        len(all_leg_values),
                    )

    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# CLI and Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Plan multi-leg routes for all shipments with cached GeoJSON geometries.",
    )
    p.add_argument(
        "--ors-api-key",
        default=os.getenv("ORS_API_KEY", ""),
        help="OpenRouteService API key for road routing (optional, uses fallback if omitted)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process only first N shipments (0 = all).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size for database inserts (default: 1000)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute routes only; do not write to database.",
    )
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    db_url = get_db_url()
    engine = create_async_engine(db_url)
    loc_tbl, shipments_tbl, legs_tbl = _resolve_tables()

    try:
        async with engine.connect() as conn:
            locations_map = await load_locations_map(conn, loc_tbl)
            log.info("Loaded %d locations with valid coordinates", len(locations_map))

            shipments = await load_shipments_to_plan(conn, shipments_tbl, limit=args.limit)
            log.info("Loaded %d shipments to plan", len(shipments))
    finally:
        await engine.dispose()

    if not shipments:
        log.warning("No shipments found. Run scripts/04_consolidate_shipments.py first.")
        return 1

    planner = RoutePlanner(road_router=RoadRouter(api_key=args.ors_api_key or None))

    plans: list[RoutePlan] = []
    skipped = 0

    log.info("Computing multi-leg route geometries for %d shipments...", len(shipments))
    for s in shipments:
        orig = locations_map.get(s["origin_id"])
        dest = locations_map.get(s["destination_id"])
        if not orig or not dest:
            skipped += 1
            continue

        plan = planner.build_plan(
            shipment_id=s["id"],
            primary_mode=s["primary_mode"],
            origin=orig,
            dest=dest,
            planned_departure=s["planned_departure"],
            cargo_weight_kg=15000.0 * s["container_count"],
            route_version=1,
        )
        plans.append(plan)

    total_legs = sum(len(p.legs) for p in plans)
    log.info(
        "Successfully planned %d shipments (%d total legs, skipped %d due to missing coords)",
        len(plans),
        total_legs,
        skipped,
    )

    if args.dry_run:
        log.info("Dry-run mode enabled — no records written to database.")
        return 0

    await persist_plans(plans, legs_tbl, batch_size=args.batch_size)
    log.info(
        "Done: persisted %d legs across %d shipments (provenance=%s)",
        total_legs,
        len(plans),
        PROVENANCE,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except Exception as exc:
        log.exception("Route planning failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
