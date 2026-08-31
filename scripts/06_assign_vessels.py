#!/usr/bin/env python3
"""
06_assign_vessels.py
====================
T-019 (Phase 1, Data Engineer) — Assigns real vessel MMSIs to sea legs.

DEPENDS ON: scripts/05_plan_routes.py (T-018) — the `legs` table must exist with
transport_mode == 'SEA' legs.

Run order contract:
    1.  scripts/02_ingest_unlocode.py
    2.  scripts/03_ingest_port_data.py
    3.  scripts/01_ingest_dataco.py
    4.  scripts/04_consolidate_shipments.py
    5.  scripts/05_plan_routes.py
    6.  scripts/06_assign_vessels.py   <-- YOU ARE HERE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("nexafreight.assign_vessels")

PROVENANCE = "CALIBRATED"
SOURCE = "VESSEL_CATALOG"


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
        from nexafreight.models.vessel import Vessel  # type: ignore

        return Location.__table__, Vessel.__table__, Leg.__table__
    except Exception:
        meta = MetaData()
        loc_tbl = Table(
            "locations",
            meta,
            Column("id", Integer, primary_key=True),
            Column("locode", String(10)),
            Column("country_code", String(2)),
        )
        vessels_tbl = Table(
            "vessels",
            meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("mmsi", Integer, unique=True, nullable=False, index=True),
            Column("name", String(255), nullable=False),
            Column("call_sign", String(20), nullable=True),
            Column("typical_lanes_json", Text, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
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
            Column("vessel_id", Integer, nullable=True),
        )
        return loc_tbl, vessels_tbl, legs_tbl


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
        _REPO_ROOT / "data" / "raw" / "vessels" / "vessel_catalog.json",
        _REPO_ROOT.parent / "data" / "raw" / "vessels" / "vessel_catalog.json",
    ]
    for c in candidates:
        p = Path(c)
        if p.is_file():
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(f"Vessel catalog not found in candidates: {candidates}")


async def amain(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog)
    log.info("Loaded catalog with %d trade lanes and vessel templates", len(catalog))

    db_url = get_db_url()
    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    loc_tbl, vessels_tbl, legs_tbl = _resolve_tables()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(vessels_tbl.create, checkfirst=True)
            await conn.run_sync(legs_tbl.create, checkfirst=True)

            now = datetime.now(UTC)

            # 1. Upsert vessels into vessels table
            mmsi_to_id: dict[int, int] = {}
            for lane, vessels in catalog.items():
                for v in vessels:
                    mmsi = int(v["mmsi"])
                    stmt = (
                        sqlite_insert(vessels_tbl)
                        .values(
                            mmsi=mmsi,
                            name=str(v.get("name", f"VESSEL-{mmsi}")),
                            call_sign=str(v.get("call_sign", "")),
                            typical_lanes_json=json.dumps([lane]),
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_update(
                            index_elements=["mmsi"],
                            set_={
                                "name": sqlite_insert(vessels_tbl).excluded.name,
                                "call_sign": sqlite_insert(vessels_tbl).excluded.call_sign,
                                "typical_lanes_json": sqlite_insert(
                                    vessels_tbl
                                ).excluded.typical_lanes_json,
                                "updated_at": now,
                            },
                        )
                    )
                    await conn.execute(stmt)

            res = await conn.execute(select(vessels_tbl.c["mmsi"], vessels_tbl.c["id"]))
            for r in res.fetchall():
                mmsi_to_id[int(r[0])] = r[1]
            log.info("Resolved %d vessels in database", len(mmsi_to_id))

            # 2. Map location IDs to country codes
            res = await conn.execute(select(loc_tbl.c["id"], loc_tbl.c["country_code"]))
            loc_country: dict[int, str] = {
                row[0]: str(row[1] or "US").upper() for row in res.fetchall()
            }

            # 3. Find all sea legs that need vessel assignment
            res = await conn.execute(
                select(
                    legs_tbl.c["id"], legs_tbl.c["origin_id"], legs_tbl.c["destination_id"]
                ).where(legs_tbl.c["transport_mode"] == "SEA")
            )
            sea_legs = res.fetchall()
            log.info("Found %d SEA legs to assign vessels to", len(sea_legs))

            if not sea_legs:
                log.warning("No SEA legs found. Run scripts/05_plan_routes.py first.")
                return 0

            # 4. Group legs by lane and assign round-robin
            default_vessels = catalog.get("DEFAULT", [])
            assignments: list[tuple[int, int]] = []  # (leg_id, vessel_id)
            used_mmsis: set[int] = set()

            lane_counters: dict[str, int] = {}

            for leg_id, orig_id, dest_id in sea_legs:
                orig_cc = loc_country.get(orig_id, "US")
                dest_cc = loc_country.get(dest_id, "NL")
                lane_key = f"{orig_cc}-{dest_cc}"

                vessels_pool = catalog.get(lane_key) or default_vessels
                if not vessels_pool:
                    vessels_pool = default_vessels

                idx = lane_counters.get(lane_key, 0)
                vessel_meta = vessels_pool[idx % len(vessels_pool)]
                lane_counters[lane_key] = idx + 1

                mmsi = int(vessel_meta["mmsi"])
                vid = mmsi_to_id.get(mmsi)
                if vid:
                    assignments.append((leg_id, vid))
                    used_mmsis.add(mmsi)

            log.info(
                "Prepared vessel assignments for %d sea legs across %d unique vessels",
                len(assignments),
                len(used_mmsis),
            )

            if args.dry_run:
                log.info("Dry-run mode enabled — no leg records updated.")
                return 0

            # 5. Update legs with vessel_id in batches
            batch_size = 1000
            for i in range(0, len(assignments), batch_size):
                chunk = assignments[i : i + batch_size]
                for leg_id, vid in chunk:
                    await conn.execute(
                        update(legs_tbl).where(legs_tbl.c["id"] == leg_id).values(vessel_id=vid)
                    )

            log.info(
                "Successfully assigned %d sea legs to %d vessels (provenance=%s, source=%s)",
                len(assignments),
                len(used_mmsis),
                PROVENANCE,
                SOURCE,
            )
            print("\n=== Active AISStream Vessel Subscription Set ===")
            print(f"Total Unique MMSIs: {len(used_mmsis)}")
            print(f"MMSI List for FiltersShipMMSI: {sorted(list(used_mmsis))}\n")

    finally:
        await engine.dispose()

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Assign real vessel MMSIs to sea legs.")
    p.add_argument("--catalog", default=None, help="Path to vessel catalog JSON")
    p.add_argument("--dry-run", action="store_true", help="Dry run mode")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except Exception as exc:
        log.exception("Assign vessels failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
