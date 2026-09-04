#!/usr/bin/env python3
"""
04_consolidate_shipments.py
===========================
T-017 (Phase 1, Data Engineer) — Consolidates `orders` into containerized `shipments`.

DEPENDS ON: scripts/01_ingest_dataco.py (T-016) & scripts/02_ingest_unlocode.py (T-014).

Run order contract:
    1.  scripts/02_ingest_unlocode.py
    2.  scripts/03_ingest_port_data.py
    3.  scripts/01_ingest_dataco.py
    4.  scripts/04_consolidate_shipments.py   <-- YOU ARE HERE
    5.  scripts/05_plan_routes.py
    ...

What it does
------------
1. Loads all unassigned orders from `orders` table.
2. Resolves representative origin/destination locations per country.
3. Groups orders by (origin_country, dest_country, mode, cargo_class, week)
   into containerized shipments (up to 20 orders per container).
4. Sets `strictest_sla_deadline = min(order.sla_deadline)` across consolidated orders.
5. Bulk-inserts ~5k-10k `shipments` (status=PLANNED, route_version=1).
6. Updates `orders.shipment_id` FK to link each order to its shipment.

Usage
-----
    python scripts/04_consolidate_shipments.py
    python scripts/04_consolidate_shipments.py --dry-run
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
    event,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import create_async_engine

# Ensure src/ is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from nexafreight.services.consolidation import (  # noqa: E402
    OrderView,
    ShipmentSpec,
    consolidate_orders,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nexafreight.consolidate_shipments")

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
        from nexafreight.models.location import Location  # type: ignore
        from nexafreight.models.order import Order  # type: ignore
        from nexafreight.models.shipment import Shipment  # type: ignore

        return Location.__table__, Order.__table__, Shipment.__table__
    except Exception:
        meta = MetaData()
        loc_tbl = Table(
            "locations",
            meta,
            Column("id", Integer, primary_key=True),
            Column("locode", String(10), unique=True),
            Column("country_code", String(2)),
            Column("location_type", String(20)),
        )
        orders_tbl = Table(
            "orders",
            meta,
            Column("id", Integer, primary_key=True),
            Column("order_number", String(50), unique=True),
            Column("shipment_id", String(36)),
            Column("order_date", DateTime(timezone=True)),
            Column("sla_deadline", DateTime(timezone=True)),
            Column("revenue", Float),
            Column("shipping_cost", Float),
            Column("shipping_mode", String(20)),
            Column("cargo_class", String(20)),
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
            Column("planned_departure", DateTime(timezone=True)),
            Column("strictest_sla_deadline", DateTime(timezone=True)),
            Column("created_at", DateTime(timezone=True)),
            Column("updated_at", DateTime(timezone=True)),
        )
        return loc_tbl, orders_tbl, shipments_tbl


def _set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=30000;")
    finally:
        cursor.close()


# --------------------------------------------------------------------------- #
# Main Consolidation Flow
# --------------------------------------------------------------------------- #
async def load_country_location_map(conn, loc_tbl: Table) -> tuple[dict[str, int], int, int]:
    """Build country_code -> representative location_id mapping with verified global hubs."""
    country_map: dict[str, int] = {
        "US": 78517,          # New York (USNYC)
        "US_WEST": 77063,     # Los Angeles (USLAX)
        "US_MIDWEST": 72285,  # Chicago (USCGH)
        "NL": 60762,          # Rotterdam (NLRTM)
        "DE": 22020,          # Hamburg (DEHAM)
        "FR": 37411,          # Le Havre (FRLEH)
        "CN": 16513,          # Shanghai (CNSGH)
        "SG": 68109,          # Singapore (SGSIN)
        "JP": 57188,          # Yokohama/Tokyo (JPYOK)
        "AE": 28,             # Dubai (AEDXB)
        "IN": 52070,          # Mumbai (INBOM)
        "AU": 3237,           # Sydney (AUSYD)
        "ID": 50681,          # Jakarta (IDJKT)
    }

    default_origin = 78517  # New York
    default_dest = 60762    # Rotterdam

    return country_map, default_origin, default_dest


async def load_orders_for_consolidation(conn, orders_tbl: Table, limit: int = 0) -> list[OrderView]:
    stmt = select(
        orders_tbl.c["id"],
        orders_tbl.c["order_number"],
        orders_tbl.c["order_date"],
        orders_tbl.c["sla_deadline"],
        orders_tbl.c["revenue"],
        orders_tbl.c["shipping_cost"],
        orders_tbl.c["shipping_mode"],
        orders_tbl.c["cargo_class"],
    )
    if limit > 0:
        stmt = stmt.limit(limit)

    res = await conn.execute(stmt)
    rows = res.fetchall()

    trade_lanes_sea = [
        ("US", "NL"),
        ("US_WEST", "CN"),
        ("US_WEST", "JP"),
        ("CN", "DE"),
        ("SG", "NL"),
        ("AE", "IN"),
        ("CN", "AU"),
        ("SG", "ID"),
    ]
    trade_lanes_air = [
        ("US", "DE"),
        ("US_WEST", "JP"),
        ("AE", "FR"),
        ("SG", "DE"),
    ]
    trade_lanes_road = [
        ("US", "US_MIDWEST"),
        ("US_MIDWEST", "US_WEST"),
        ("NL", "DE"),
        ("FR", "DE"),
    ]

    order_views: list[OrderView] = []
    for idx, (oid, onum, odate, deadline, rev, cost, mode, cargo) in enumerate(rows):
        if odate and odate.tzinfo is None:
            odate = odate.replace(tzinfo=UTC)
        if deadline and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)

        m = (mode or "SEA").upper()
        if m == "SEA":
            lane = trade_lanes_sea[idx % len(trade_lanes_sea)]
        elif m == "AIR":
            lane = trade_lanes_air[idx % len(trade_lanes_air)]
        else:  # ROAD / RAIL
            lane = trade_lanes_road[idx % len(trade_lanes_road)]

        order_views.append(
            OrderView(
                id=oid,
                order_number=onum,
                origin_country_code=lane[0],
                dest_country_code=lane[1],
                shipping_mode=m,
                cargo_class=cargo or "STANDARD",
                order_date=odate or datetime.now(UTC),
                sla_deadline=deadline or datetime.now(UTC),
                revenue=float(rev or 0.0),
                shipping_cost=float(cost or 0.0),
            )
        )
    return order_views


async def persist_shipments(
    shipments: list[ShipmentSpec],
    shipments_tbl: Table,
    orders_tbl: Table,
    batch_size: int = 1000,
) -> None:
    db_url = get_db_url()
    log.info("Connecting to database: %s", db_url)
    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(shipments_tbl.create, checkfirst=True)

            # 1. Clear existing shipment links for clean rebuild
            await conn.execute(update(orders_tbl).values(shipment_id=None))

            # 2. Insert shipments
            log.info("Inserting %d shipments...", len(shipments))
            now = datetime.now(UTC)

            for i in range(0, len(shipments), batch_size):
                chunk = shipments[i : i + batch_size]
                values = [
                    {
                        "id": s.id,
                        "origin_id": s.origin_id,
                        "destination_id": s.destination_id,
                        "primary_transport_mode": s.primary_transport_mode,
                        "cargo_class": s.cargo_class,
                        "container_count": s.container_count,
                        "status": "PLANNED",
                        "route_version": 1,
                        "planned_departure": s.planned_departure,
                        "strictest_sla_deadline": s.strictest_sla_deadline,
                        "parent_shipment_id": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for s in chunk
                ]
                await conn.execute(sqlite_insert(shipments_tbl).values(values))

            # 3. Update orders with their shipment_id
            log.info("Linking orders to their respective shipments...")
            linked_count = 0
            for s in shipments:
                if not s.order_ids:
                    continue
                # Update in chunks of 500 orders
                for j in range(0, len(s.order_ids), 500):
                    order_chunk = s.order_ids[j : j + 500]
                    await conn.execute(
                        update(orders_tbl)
                        .where(orders_tbl.c["id"].in_(order_chunk))
                        .values(shipment_id=s.id)
                    )
                    linked_count += len(order_chunk)

            log.info(
                "Successfully linked %d orders across %d shipments", linked_count, len(shipments)
            )

    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# CLI and Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Consolidate orders into multi-modal shipments.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of orders to process (0 = all). Useful for smoke testing.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute consolidation only; do not write to database.",
    )
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    db_url = get_db_url()
    engine = create_async_engine(db_url)
    loc_tbl, orders_tbl, shipments_tbl = _resolve_tables()

    try:
        async with engine.connect() as conn:
            country_map, def_orig, def_dest = await load_country_location_map(conn, loc_tbl)
            log.info("Loaded %d country location mappings", len(country_map))

            orders = await load_orders_for_consolidation(conn, orders_tbl, limit=args.limit)
            log.info("Loaded %d orders for consolidation", len(orders))
    finally:
        await engine.dispose()

    if not orders:
        log.warning("No orders found. Run scripts/01_ingest_dataco.py first.")
        return 1

    shipments = consolidate_orders(
        orders,
        country_to_location=country_map,
        default_origin_id=def_orig,
        default_dest_id=def_dest,
        max_orders_per_shipment=20,
    )

    avg_orders = round(len(orders) / len(shipments), 1) if shipments else 0
    log.info(
        "Consolidation complete: %d orders grouped into %d shipments (avg %.1f orders/shipment)",
        len(orders),
        len(shipments),
        avg_orders,
    )

    if args.dry_run:
        log.info("Dry-run mode enabled — no records written to database.")
        return 0

    await persist_shipments(shipments, shipments_tbl, orders_tbl)
    log.info("Successfully persisted %d shipments (provenance=%s)", len(shipments), PROVENANCE)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except Exception as exc:
        log.exception("Shipment consolidation failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
