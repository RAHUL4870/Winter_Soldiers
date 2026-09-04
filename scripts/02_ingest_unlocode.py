#!/usr/bin/env python3
"""
02_ingest_unlocode.py
=====================
T-014 (Phase 1, Data Engineer) — Ingests the UNECE UN/LOCODE dataset into the
`locations` table. This is the FIRST script to run; every other ingest script
(01 dataco, 03 ports, 04 consolidation, 05 routes) depends on `locations`.

Run order contract:
    1.  scripts/02_ingest_unlocode.py   <-- YOU ARE HERE
    2.  scripts/03_ingest_port_data.py
    3.  scripts/01_ingest_dataco.py
    4.  scripts/04_consolidate_shipments.py
    ...

What it does
------------
1. Reads the UNECE UN/LOCODE CSV (data/raw/unlocode/unlocode.csv or directory of parts).
2. Parses 5-character LOCODE (Country code [2 chars] + Location code [3 chars]).
3. Converts Coordinates from DDMM[NS] DDDMM[EW] format into signed decimal degrees (WGS84).
4. Filters and classifies into domain LocationType:
   - Sea port ("1" in Function) -> PORT
   - Airport ("4" in Function) -> AIRPORT
   - Rail ("2" in Function), Road ("3" in Function), ICD/dry port -> INLAND_DEPOT
   - Warehouse / logistics hub / CFS -> WAREHOUSE
5. Bulk-inserts / upserts ~84k+ rows into `locations` table idempotently on `locode`.

Usage
-----
    python scripts/02_ingest_unlocode.py
    python scripts/02_ingest_unlocode.py --input data/raw/unlocode/unlocode.csv
    python scripts/02_ingest_unlocode.py --limit 100 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore
from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table, event
from sqlalchemy.dialects.sqlite import insert
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
log = logging.getLogger("nexafreight.ingest_unlocode")

PROVENANCE = "CALIBRATED"
SOURCE = "UNECE_UNLOCODE"

# LocationType constants matching src.nexafreight.enums.LocationType
PORT = "PORT"
AIRPORT = "AIRPORT"
INLAND_DEPOT = "INLAND_DEPOT"
WAREHOUSE = "WAREHOUSE"


# --------------------------------------------------------------------------- #
# Database URL resolution
# --------------------------------------------------------------------------- #
def get_db_url() -> str:
    """Retrieve async database URL from project settings or fallback."""
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


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
class LocationRow:
    __slots__ = ("locode", "name", "country_code", "latitude", "longitude", "location_type")

    def __init__(
        self,
        locode: str,
        name: str,
        country_code: str,
        latitude: float,
        longitude: float,
        location_type: str,
    ) -> None:
        self.locode = locode
        self.name = name[:255]  # respect VARCHAR(255)
        self.country_code = country_code[:2]
        self.latitude = latitude
        self.longitude = longitude
        self.location_type = location_type


class ParseStats:
    def __init__(self) -> None:
        self.rows_read: int = 0
        self.coords_ok: int = 0
        self.coords_skipped: int = 0
        self.filtered_out: int = 0
        self.kept: int = 0
        self.inserted: int = 0
        self.duplicates_collapsed: int = 0


# --------------------------------------------------------------------------- #
# Coordinate Parsing
# --------------------------------------------------------------------------- #
_LAT_RE = re.compile(r"^\s*(\d{2,3})(\d{2})\s*([NS])\s*$", re.IGNORECASE)
_LON_RE = re.compile(r"^\s*(\d{2,3})(\d{2})\s*([EW])\s*$", re.IGNORECASE)


def _coord_to_decimal(value: int, hemisphere: str, kind: str) -> float:
    degrees = value // 100
    minutes = value % 100
    if minutes >= 60:
        raise ValueError(f"minutes={minutes} out of range for {kind}")
    decimal = degrees + minutes / 60.0
    sign = -1.0 if hemisphere.upper() in ("S", "W") else 1.0
    return round(sign * decimal, 6)


def parse_coordinates(coords: str) -> tuple[float, float] | None:
    """Parse 'DDMM[NS] DDDMM[EW]' into (latitude, longitude) decimal degrees."""
    if not coords or not isinstance(coords, str):
        return None

    parts = coords.strip().split()
    if len(parts) < 2:
        return None

    lat_m = _LAT_RE.match(parts[0])
    lon_m = _LON_RE.match(parts[1])
    if not lat_m or not lon_m:
        return None

    try:
        lat = _coord_to_decimal(int(lat_m.group(1) + lat_m.group(2)), lat_m.group(3), "lat")
        lon = _coord_to_decimal(int(lon_m.group(1) + lon_m.group(2)), lon_m.group(3), "lon")
    except ValueError:
        return None

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return (lat, lon)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
_ICD_KEYWORDS = (
    "icd",
    "inland container",
    "container depot",
    "dry port",
    "depot",
)
_WAREHOUSE_KEYWORDS = (
    "warehouse",
    "distribution center",
    "distribution centre",
    "logistics hub",
    "logistics park",
    "freight village",
    "cfs",
    "container freight",
)


def classify_location(function: str, name: str, remarks: str) -> str | None:
    """Classify into PORT, AIRPORT, INLAND_DEPOT, or WAREHOUSE.

    Returns None if not a relevant freight transport node.
    """
    f = (function or "").strip()
    n = (name or "").lower()
    r = (remarks or "").lower()

    if "1" in f:
        return PORT
    if "4" in f:
        return AIRPORT
    if "2" in f or "3" in f:
        return INLAND_DEPOT
    if any(k in n or k in r for k in _ICD_KEYWORDS):
        return INLAND_DEPOT
    if any(k in n or k in r for k in _WAREHOUSE_KEYWORDS):
        return WAREHOUSE
    return None


# --------------------------------------------------------------------------- #
# CSV Ingestion & Parsing
# --------------------------------------------------------------------------- #
def resolve_input_files(input_path_str: str) -> list[Path]:
    """Find CSV file(s) from a file path, directory, or known workspace locations."""
    path = Path(input_path_str)
    if path.is_file():
        return [path]
    if path.is_dir():
        csvs = sorted(path.glob("*CodeListPart*.csv")) or sorted(path.glob("*.csv"))
        if csvs:
            return csvs

    # Fallback search locations
    candidates = [
        _REPO_ROOT / "data" / "raw" / "unlocode",
        _REPO_ROOT / "data" / "raw" / "unlocode" / "unlocode.csv",
        _REPO_ROOT.parent / "data" / "raw" / "unlocode",
        _REPO_ROOT.parent / "data" / "raw" / "unlocode" / "unlocode.csv",
        _REPO_ROOT.parent / "Datasets" / "UNLOCODE — Location Codes for Ports, Airports, and ICDs",
    ]
    for cand in candidates:
        if cand.is_file():
            return [cand]
        if cand.is_dir():
            csvs = sorted(cand.glob("*CodeListPart*.csv")) or sorted(cand.glob("*.csv"))
            if csvs:
                return csvs

    raise FileNotFoundError(f"UN/LOCODE dataset not found at: {input_path_str}")


def load_dataframe(files: list[Path]) -> pd.DataFrame:
    """Load and concatenate UN/LOCODE CSV files handling various encodings & header types."""
    dfs: list[pd.DataFrame] = []

    for f in files:
        loaded = False
        for enc in ("latin-1", "utf-8-sig", "cp1252", "utf-8"):
            try:
                # First check if the file has a header row
                sample_df = pd.read_csv(f, encoding=enc, nrows=5, dtype=str)
                has_header = any(
                    c.lower() in ("locode", "country", "name", "coordinates")
                    for c in sample_df.columns
                )

                if has_header:
                    df = pd.read_csv(
                        f,
                        encoding=enc,
                        dtype=str,
                        keep_default_na=False,
                        on_bad_lines="skip",
                        low_memory=False,
                    )
                else:
                    df = pd.read_csv(
                        f,
                        encoding=enc,
                        header=None,
                        dtype=str,
                        keep_default_na=False,
                        on_bad_lines="skip",
                        low_memory=False,
                    )
                dfs.append(df)
                loaded = True
                break
            except Exception:
                continue
        if not loaded:
            raise ValueError(f"Could not load CSV file: {f}")

    if not dfs:
        raise ValueError("No dataframes loaded.")
    if len(dfs) == 1:
        return dfs[0]
    return pd.concat(dfs, ignore_index=True)


def parse_rows(df: pd.DataFrame, stats: ParseStats) -> Iterable[LocationRow]:
    """Extract and validate LocationRow items from DataFrame."""
    # Check if df has numeric columns (headerless standard UNECE 12-column format)
    is_headerless = all(isinstance(col, int) for col in df.columns)

    for _, row in df.iterrows():
        stats.rows_read += 1

        if is_headerless:
            # Col 0: Change, Col 1: Country, Col 2: Locode, Col 3: Name, Col 4: NameWoDiacritics
            # Col 5: SubDiv, Col 6: Function, Col 7: Status, Col 8: Date, Col 9: IATA
            # Col 10: Coordinates, Col 11: Remarks
            if len(row) < 11:
                continue
            country = str(row[1]).strip().upper()
            loc = str(row[2]).strip().upper()
            if len(country) != 2 or len(loc) != 3 or not (country + loc).isalnum():
                stats.coords_skipped += 1
                continue
            locode = country + loc
            name = str(row[4] or row[3] or "").strip()
            function = str(row[6] or "").strip()
            coords = str(row[10] or "").strip()
            remarks = str(row[11] or "").strip() if len(row) > 11 else ""
            lat_raw = ""
            lon_raw = ""
        else:
            # Header-based lookup (case-insensitive)
            r = {str(k).strip().lower(): str(v).strip() for k, v in row.items()}
            locode = r.get("locode", "")
            country = r.get("country", "") or r.get("country_code", "")
            loc = r.get("location", "")
            if not locode and country and loc:
                locode = country + loc
            locode = locode.upper()
            if len(locode) != 5 or not locode.isalnum():
                stats.coords_skipped += 1
                continue
            country = locode[:2]
            name = r.get("namewodiacritics") or r.get("name", "")
            function = r.get("function", "")
            coords = r.get("coordinates", "")
            remarks = r.get("remarks", "")
            lat_raw = r.get("latitude", "") or r.get("lat", "")
            lon_raw = r.get("longitude", "") or r.get("lon", "")

        location_type = classify_location(function, name, remarks)
        if location_type is None:
            stats.filtered_out += 1
            continue

        parsed = parse_coordinates(coords)
        if parsed is None and lat_raw and lon_raw:
            try:
                lat = float(lat_raw)
                lon = float(lon_raw)
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    parsed = (round(lat, 6), round(lon, 6))
            except ValueError:
                parsed = None

        if parsed is None:
            stats.coords_skipped += 1
            continue

        stats.coords_ok += 1
        stats.kept += 1
        lat, lon = parsed
        yield LocationRow(
            locode=locode,
            name=name,
            country_code=country,
            latitude=lat,
            longitude=lon,
            location_type=location_type,
        )


# --------------------------------------------------------------------------- #
# Database Upsert
# --------------------------------------------------------------------------- #
def _local_locations_table() -> Table:
    """Fallback locations table schema matching migration 001_initial_schema."""
    meta = MetaData()
    return Table(
        "locations",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("locode", String(10), unique=True, index=True, nullable=False),
        Column("name", String(255), nullable=False),
        Column("country_code", String(2), nullable=False),
        Column("location_type", String(20), nullable=False),
        Column("latitude", Float, nullable=False),
        Column("longitude", Float, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )


def _resolve_location_table() -> Table:
    try:
        from typing import cast

        from nexafreight.models.location import Location  # type: ignore

        return cast(Table, Location.__table__)
    except Exception:
        return _local_locations_table()


def _set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=30000;")
    finally:
        cursor.close()


async def upsert_locations(
    rows: list[LocationRow], stats: ParseStats, batch_size: int = 1000
) -> None:
    """Bulk upsert locations idempotently on locode into SQLite."""
    # Deduplicate rows by locode before insert to prevent SQLite single-statement conflict error
    locode_map: dict[str, LocationRow] = {}
    for r in rows:
        locode_map[r.locode] = r

    unique_rows = list(locode_map.values())
    stats.duplicates_collapsed = len(rows) - len(unique_rows)
    if stats.duplicates_collapsed > 0:
        log.info("Deduplicated %d duplicate LOCODEs in input", stats.duplicates_collapsed)

    db_url = get_db_url()
    log.info("Connecting to database: %s", db_url)
    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    table = _resolve_location_table()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(table.create, checkfirst=True)
            exc = table.c
            now = datetime.now(UTC)

            for i in range(0, len(unique_rows), batch_size):
                chunk = unique_rows[i : i + batch_size]
                values = [
                    {
                        "locode": r.locode,
                        "name": r.name,
                        "country_code": r.country_code,
                        "location_type": r.location_type,
                        "latitude": r.latitude,
                        "longitude": r.longitude,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for r in chunk
                ]
                stmt = (
                    insert(table)
                    .values(values)
                    .on_conflict_do_update(
                        index_elements=["locode"],
                        set_={
                            "name": exc["name"],
                            "country_code": exc["country_code"],
                            "location_type": exc["location_type"],
                            "latitude": exc["latitude"],
                            "longitude": exc["longitude"],
                            "updated_at": now,
                        },
                    )
                )
                await conn.execute(stmt)
                stats.inserted += len(chunk)
                log.info(
                    "Upserted batch [%d..%d] of %d rows",
                    i + 1,
                    min(i + batch_size, len(unique_rows)),
                    len(unique_rows),
                )
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# CLI and Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ingest UNECE UN/LOCODE into the locations table.",
    )
    p.add_argument(
        "--input",
        default="data/raw/unlocode/unlocode.csv",
        help="Path to the UN/LOCODE CSV or directory (default: data/raw/unlocode/unlocode.csv)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after N rows (0 = process all). Useful for testing.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size for chunked database upserts (default: 1000).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate only; do not write to the database.",
    )
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    files = resolve_input_files(args.input)
    log.info("Loading UN/LOCODE from: %s", [str(f) for f in files])
    df = load_dataframe(files)
    log.info("Loaded dataframe with %d total rows", len(df))

    if args.limit > 0:
        df = df.head(args.limit)
        log.info("Limited to %d rows for testing", args.limit)

    stats = ParseStats()
    rows = list(parse_rows(df, stats))
    log.info(
        "Parsed summary: rows_read=%d, kept=%d, filtered_out=%d, coords_ok=%d, coords_skipped=%d",
        stats.rows_read,
        stats.kept,
        stats.filtered_out,
        stats.coords_ok,
        stats.coords_skipped,
    )

    if args.dry_run:
        log.info("Dry-run mode enabled — no records written to database.")
        return 0

    if not rows:
        log.warning("No valid rows to insert.")
        return 0

    await upsert_locations(rows, stats, batch_size=args.batch_size)
    log.info(
        "Ingestion completed successfully: inserted/updated=%d, provenance=%s, source=%s",
        stats.inserted,
        PROVENANCE,
        SOURCE,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except Exception as exc:
        log.exception("UN/LOCODE ingestion failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
