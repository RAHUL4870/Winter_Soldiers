#!/usr/bin/env python3
"""
07_assign_flights.py
====================
T-020 (Phase 1, Data Engineer) — Assigns real cargo flight numbers + durations
to air legs.

DEPENDS ON: scripts/05_plan_routes.py (T-018) — the `legs` table must exist with
transport_mode == 'AIR' legs.

Run order contract:
    1.  scripts/02_ingest_unlocode.py
    2.  scripts/03_ingest_port_data.py
    3.  scripts/01_ingest_dataco.py
    4.  scripts/04_consolidate_shipments.py
    5.  scripts/05_plan_routes.py
    6.  scripts/06_assign_vessels.py
    7.  scripts/07_assign_flights.py   <-- YOU ARE HERE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    event,
    select,
    update,
)
from sqlalchemy.ext.asyncio import create_async_engine

# Ensure src/ is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nexafreight.assign_flights")

PROVENANCE = "DERIVED"
SOURCE = "FLIGHT_CATALOG"


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

        return Location.__table__, Leg.__table__
    except Exception:
        meta = MetaData()
        loc_tbl = Table(
            "locations",
            meta,
            Column("id", Integer, primary_key=True),
            Column("locode", String(10)),
            Column("country_code", String(2)),
        )
        legs_tbl = Table(
            "legs",
            meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("shipment_id", String(36), nullable=False),
            Column("sequence_number", Integer, nullable=False),
            Column("route_version", Integer, nullable=False),
            Column("transport_mode", String(20), nullable=False),
            Column("origin_id", Integer, nullable=False),
            Column("destination_id", Integer, nullable=False),
            Column("flight_number", String(20), nullable=True),
            Column("planned_departure", DateTime(timezone=True), nullable=False),
            Column("planned_arrival", DateTime(timezone=True), nullable=False),
        )
        return loc_tbl, legs_tbl


def _set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=30000;")
    finally:
        cursor.close()


def load_catalog(path: str | None = None) -> dict[str, list[dict[str, Any]]]:
    candidates: list[str | Path] = [
        path or "",
        _REPO_ROOT / "data" / "raw" / "flights" / "flight_catalog.json",
        _REPO_ROOT.parent / "data" / "raw" / "flights" / "flight_catalog.json",
    ]
    for c in candidates:
        p = Path(c)
        if p.is_file():
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(f"Flight catalog not found in candidates: {candidates}")


async def amain(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    log.info("Loaded flight catalog with %d trade lanes and flight templates", len(catalog))

    db_url = get_db_url()
    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    loc_tbl, legs_tbl = _resolve_tables()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(legs_tbl.create, checkfirst=True)

            # 1. Map location IDs to country codes
            res = await conn.execute(select(loc_tbl.c["id"], loc_tbl.c["country_code"]))
            loc_country: dict[int, str] = {
                row[0]: str(row[1] or "US").upper() for row in res.fetchall()
            }

            # 2. Find all air legs
            res = await conn.execute(
                select(
                    legs_tbl.c["id"],
                    legs_tbl.c["origin_id"],
                    legs_tbl.c["destination_id"],
                    legs_tbl.c["planned_departure"],
                ).where(legs_tbl.c["transport_mode"] == "AIR")
            )
            air_legs = res.fetchall()
            log.info("Found %d AIR legs to assign flights to", len(air_legs))

            if not air_legs:
                log.warning("No AIR legs found. Run scripts/05_plan_routes.py first.")
                return 0

            default_flights = catalog.get("DEFAULT", [])
            assignments: list[tuple[int, str, datetime]] = []
            used_flights: set[str] = set()
            lane_counters: dict[str, int] = {}

            for leg_id, orig_id, dest_id, p_dep in air_legs:
                orig_cc = loc_country.get(orig_id, "US")
                dest_cc = loc_country.get(dest_id, "NL")
                lane_key = f"{orig_cc}-{dest_cc}"

                pool = catalog.get(lane_key) or default_flights
                if not pool:
                    pool = default_flights

                idx = lane_counters.get(lane_key, 0)
                f_meta = pool[idx % len(pool)]
                lane_counters[lane_key] = idx + 1

                f_num = str(f_meta["flight_number"]).strip().upper()
                dur_h = float(f_meta.get("duration_hours", 10.0))

                if p_dep and p_dep.tzinfo is None:
                    p_dep = p_dep.replace(tzinfo=UTC)
                new_arr = (p_dep or datetime.now(UTC)) + timedelta(hours=dur_h)

                assignments.append((leg_id, f_num, new_arr))
                used_flights.add(f_num)

            log.info(
                "Prepared flight assignments for %d air legs across %d unique flight numbers",
                len(assignments),
                len(used_flights),
            )

            if args.dry_run:
                log.info("Dry-run mode enabled — no leg records updated.")
                return 0

            batch_size = 1000
            for i in range(0, len(assignments), batch_size):
                chunk = assignments[i : i + batch_size]
                for leg_id, f_num, new_arr in chunk:
                    await conn.execute(
                        update(legs_tbl)
                        .where(legs_tbl.c["id"] == leg_id)
                        .values(flight_number=f_num, planned_arrival=new_arr)
                    )

            log.info(
                "Successfully assigned %d air legs to %d flight numbers (provenance=%s, source=%s)",
                len(assignments),
                len(used_flights),
                PROVENANCE,
                SOURCE,
            )
            print("\n=== Active Cargo Flights Assigned ===")
            print(f"Total Unique Flight Numbers: {len(used_flights)}")
            print(f"Flight Numbers: {sorted(list(used_flights))}\n")

    finally:
        await engine.dispose()

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Assign cargo flight numbers + durations to air legs.")
    p.add_argument("--catalog", default=None, help="Path to flight catalog JSON")
    p.add_argument("--dry-run", action="store_true", help="Dry run mode")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except Exception as exc:
        log.exception("Assign flights failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
