#!/usr/bin/env python3
"""
03_ingest_port_data.py
======================
T-015 (Phase 1, Data Engineer) — Ingests port activity + maritime port
performance data and builds the `ports` and `port_daily_stats` tables.

DEPENDS ON: scripts/02_ingest_unlocode.py (T-014) — the `locations` table must
already be populated so port names can be fuzzy-matched to UN/LOCODE codes.

Run order contract:
    1.  scripts/02_ingest_unlocode.py
    2.  scripts/03_ingest_port_data.py   <-- YOU ARE HERE
    3.  scripts/01_ingest_dataco.py
    4.  scripts/04_consolidate_shipments.py
    ...

What it does
------------
1. Reads IMF Daily Port Activity (daily vessel calls per port) and Maritime
   Port Performance data.
2. Fuzzy-matches each port name to an existing UN/LOCODE row in `locations`
   (exact match -> World Port Index alias -> RapidFuzz token_set_ratio).
3. Computes a 90-day rolling baseline vessel count per port.
4. Computes daily `congestion_index = vessel_count / rolling_90d_avg`.
5. Populates the `ports` table (linked 1-to-1 with `locations.id`) and
   `port_daily_stats` (daily congestion index per port).

Usage
-----
    python scripts/03_ingest_port_data.py
    python scripts/03_ingest_port_data.py --start 2024-01-01 --end 2024-03-31
    python scripts/03_ingest_port_data.py --limit 1000 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections import deque
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import create_async_engine

try:
    from rapidfuzz import fuzz, process  # type: ignore

    _HAS_RAPIDFUZZ = True
except Exception:
    _HAS_RAPIDFUZZ = False

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
log = logging.getLogger("nexafreight.ingest_port_data")

PROVENANCE = "CALIBRATED"
SOURCE_ACTIVITY = "IMF_PORT_ACTIVITY"
SOURCE_PERFORMANCE = "KAGGLE_PORT_PERFORMANCE"

DEFAULT_CONGESTION_THRESHOLD = 1.4
DEFAULT_DEMURRAGE_FREE_DAYS = 3
DEFAULT_DEMURRAGE_RATE_USD = 150.0
DEFAULT_DWELL_HOURS = 48.0


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


# --------------------------------------------------------------------------- #
# Data models & structures
# --------------------------------------------------------------------------- #
class DailyStat:
    __slots__ = (
        "port_identifier",
        "stat_date",
        "vessel_count",
        "rolling_90d_avg",
        "congestion_index",
    )

    def __init__(
        self,
        port_identifier: str,
        stat_date: date,
        vessel_count: float,
        rolling_90d_avg: float | None = None,
        congestion_index: float | None = None,
    ) -> None:
        self.port_identifier = port_identifier
        self.stat_date = stat_date
        self.vessel_count = vessel_count
        self.rolling_90d_avg = rolling_90d_avg
        self.congestion_index = congestion_index


class ParseStats:
    def __init__(self) -> None:
        self.activity_rows: int = 0
        self.matched_ports: int = 0
        self.unmatched_ports: int = 0
        self.ports_created: int = 0
        self.daily_stats_inserted: int = 0


# --------------------------------------------------------------------------- #
# File resolution
# --------------------------------------------------------------------------- #
def resolve_file(candidate_paths: list[Path | str], description: str) -> Path:
    for cand in candidate_paths:
        p = Path(cand)
        if p.is_file():
            return p
        if p.is_dir():
            csvs = list(p.glob("*.csv"))
            if csvs:
                return csvs[0]
    raise FileNotFoundError(f"Could not locate {description} at candidates: {candidate_paths}")


def get_activity_file(user_path: str | None) -> Path:
    cands: list[str | Path] = [
        user_path or "",
        _REPO_ROOT / "data" / "raw" / "port_activity" / "port_activity.csv",
        _REPO_ROOT.parent.parent
        / "Datasets"
        / "IMF Daily Port Activity"
        / "Daily_Port_Activity_Data_and_Trade_Estimates.csv",
        _REPO_ROOT.parent
        / "Datasets"
        / "IMF Daily Port Activity"
        / "Daily_Port_Activity_Data_and_Trade_Estimates.csv",
        _REPO_ROOT
        / "Datasets"
        / "IMF Daily Port Activity"
        / "Daily_Port_Activity_Data_and_Trade_Estimates.csv",
    ]
    return resolve_file([c for c in cands if c], "IMF Port Activity CSV")


def get_performance_file(user_path: str | None) -> Path:
    cands: list[str | Path] = [
        user_path or "",
        _REPO_ROOT / "data" / "raw" / "port_performance" / "maritime_port_performance.csv",
        _REPO_ROOT.parent.parent
        / "Datasets"
        / "Maritime Port Performance Dataset"
        / "Maritime Port Performance Project Dataset.csv",
        _REPO_ROOT.parent
        / "Datasets"
        / "Maritime Port Performance Dataset"
        / "Maritime Port Performance Project Dataset.csv",
        _REPO_ROOT
        / "Datasets"
        / "Maritime Port Performance Dataset"
        / "Maritime Port Performance Project Dataset.csv",
    ]
    return resolve_file([c for c in cands if c], "Maritime Port Performance CSV")


def get_wpi_file() -> Path | None:
    cands = [
        _REPO_ROOT.parent.parent / "Datasets" / "World Port Index" / "UpdatedPub150.csv",
        _REPO_ROOT.parent / "Datasets" / "World Port Index" / "UpdatedPub150.csv",
        _REPO_ROOT / "Datasets" / "World Port Index" / "UpdatedPub150.csv",
        _REPO_ROOT / "data" / "raw" / "world_port_index" / "UpdatedPub150.csv",
    ]
    for c in cands:
        if c.is_file():
            return c
    return None


# --------------------------------------------------------------------------- #
# Date & Column Parsing
# --------------------------------------------------------------------------- #
def _parse_date(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    v = str(value).strip()
    if not v:
        return None
    # Truncate time components
    if " " in v:
        v = v.split(" ")[0]
    if "T" in v:
        v = v.split("T")[0]
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _load_activity_data(
    file_path: Path,
    start_date: date | None,
    end_date: date | None,
    limit: int,
    stats: ParseStats,
) -> list[DailyStat]:
    """Stream or load IMF Port Activity CSV efficiently."""
    log.info("Loading port activity from: %s", file_path)
    # Read headers to locate column names
    header_df = pd.read_csv(file_path, nrows=5, encoding="utf-8-sig")
    date_col = next((c for c in header_df.columns if "date" in c.lower()), header_df.columns[0])
    if "portname" in header_df.columns:
        port_col = "portname"
    else:
        port_col = next(
            (c for c in header_df.columns if "port" in c.lower() and "id" not in c.lower()),
            "portname",
        )
    if "portcalls" in header_df.columns:
        calls_col = "portcalls"
    else:
        calls_col = next(
            (c for c in header_df.columns if "calls" in c.lower() or "count" in c.lower()),
            "portcalls",
        )

    log.info(
        "Using columns: date='%s', port='%s', vessel_calls='%s'", date_col, port_col, calls_col
    )

    chunksize = 100_000
    rows: list[DailyStat] = []

    for chunk in pd.read_csv(
        file_path,
        usecols=[date_col, port_col, calls_col],
        encoding="utf-8-sig",
        dtype=str,
        chunksize=chunksize,
        low_memory=False,
    ):
        stats.activity_rows += len(chunk)
        for _, r in chunk.iterrows():
            p_name = str(r.get(port_col, "")).strip()
            if not p_name or p_name.lower() in ("nan", "none", ""):
                continue

            d = _parse_date(r.get(date_col))
            if d is None:
                continue
            if start_date and d < start_date:
                continue
            if end_date and d > end_date:
                continue

            try:
                calls = float(r.get(calls_col, 0) or 0)
            except (ValueError, TypeError):
                calls = 0.0

            rows.append(DailyStat(port_identifier=p_name, stat_date=d, vessel_count=calls))

            if limit > 0 and len(rows) >= limit:
                break
        if limit > 0 and len(rows) >= limit:
            break

    log.info("Loaded %d activity records (filtered by date/limit)", len(rows))
    return rows


# --------------------------------------------------------------------------- #
# Matching: Port Names -> DB Location IDs
# --------------------------------------------------------------------------- #
def build_port_matcher(db_ports: list[tuple[int, str, str, str]], wpi_file: Path | None):
    """Build fast exact and fuzzy matcher against DB locations.

    db_ports: [(location_id, locode, name, country_code), ...]
    """
    exact_map: dict[str, tuple[int, str]] = {}
    locode_map: dict[str, tuple[int, str]] = {}
    names_list: list[str] = []
    name_to_target: dict[str, tuple[int, str]] = {}

    for loc_id, locode, name, _cc in db_ports:
        n_clean = name.strip().lower()
        exact_map[n_clean] = (loc_id, locode)
        locode_map[locode.upper()] = (loc_id, locode)
        names_list.append(n_clean)
        name_to_target[n_clean] = (loc_id, locode)

    # Optional World Port Index alias mapping
    wpi_alias_map: dict[str, tuple[int, str]] = {}
    if wpi_file and wpi_file.is_file():
        try:
            wpi_df = pd.read_csv(wpi_file, encoding="latin-1", dtype=str)
            for _, r in wpi_df.iterrows():
                p_name = str(r.get("Main Port Name", "")).strip().lower()
                unloc = str(r.get("UN/LOCODE", "")).strip().upper()
                if p_name and unloc in locode_map:
                    wpi_alias_map[p_name] = locode_map[unloc]
            log.info("Loaded %d World Port Index alias mappings", len(wpi_alias_map))
        except Exception as exc:
            log.warning("Could not parse World Port Index aliases: %s", exc)

    def match_port(port_name: str, threshold: float = 0.80) -> tuple[int, str] | None:
        p_clean = port_name.strip().lower()
        if not p_clean:
            return None

        # 1. Exact locode
        if p_clean.upper() in locode_map:
            return locode_map[p_clean.upper()]

        # 2. Exact name match
        if p_clean in exact_map:
            return exact_map[p_clean]

        # 3. WPI alias match
        if p_clean in wpi_alias_map:
            return wpi_alias_map[p_clean]

        # 4. Fuzzy match
        if _HAS_RAPIDFUZZ and names_list:
            res = process.extractOne(p_clean, names_list, scorer=fuzz.token_set_ratio)
            if res and (res[1] / 100.0) >= threshold:
                best_name = res[0]
                return name_to_target[best_name]

        return None

    return match_port


# --------------------------------------------------------------------------- #
# Rolling Congestion Calculation
# --------------------------------------------------------------------------- #
def compute_congestion(daily_stats: Sequence[DailyStat]) -> list[DailyStat]:
    """Calculate 90-day rolling baseline vessel calls and congestion index."""
    by_port: dict[str, list[DailyStat]] = {}
    for row in daily_stats:
        by_port.setdefault(row.port_identifier, []).append(row)

    calculated: list[DailyStat] = []

    for _p_name, rows in by_port.items():
        rows.sort(key=lambda r: r.stat_date)
        q: deque[tuple[date, float]] = deque()

        for r in rows:
            cutoff = r.stat_date - timedelta(days=90)
            while q and q[0][0] < cutoff:
                q.popleft()
            q.append((r.stat_date, r.vessel_count))

            avg = sum(x[1] for x in q) / len(q)
            r.rolling_90d_avg = round(avg, 4)
            r.congestion_index = round(r.vessel_count / avg, 4) if avg > 0 else 1.0
            calculated.append(r)

    return calculated


# --------------------------------------------------------------------------- #
# Database Tables & Persistence
# --------------------------------------------------------------------------- #
def _resolve_tables():
    try:
        from nexafreight.models.port import Port, PortDailyStat  # type: ignore

        return Port.__table__, PortDailyStat.__table__
    except Exception:
        meta = MetaData()
        port_tbl = Table(
            "ports",
            meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column(
                "location_id",
                Integer,
                ForeignKey("locations.id", ondelete="RESTRICT"),
                unique=True,
                nullable=False,
            ),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        daily_tbl = Table(
            "port_daily_stats",
            meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("port_id", Integer, ForeignKey("ports.id", ondelete="CASCADE"), nullable=False),
            Column("stat_date", Date, nullable=False, index=True),
            Column("congestion_index", Float, nullable=False),
            UniqueConstraint("port_id", "stat_date", name="uq_port_daily_stats_port_id_stat_date"),
        )
        return port_tbl, daily_tbl


def _set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=30000;")
    finally:
        cursor.close()


async def populate_database(
    matched_port_locations: set[int],
    daily_stats_by_loc_id: list[tuple[int, date, float]],
    stats: ParseStats,
    batch_size: int = 1000,
) -> None:
    db_url = get_db_url()
    log.info("Connecting to database: %s", db_url)
    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    port_tbl, daily_tbl = _resolve_tables()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(port_tbl.create, checkfirst=True)
            await conn.run_sync(daily_tbl.create, checkfirst=True)

            now = datetime.now(UTC)

            # 1. Upsert ports for all matched location IDs
            log.info("Upserting %d ports into 'ports' table...", len(matched_port_locations))
            for loc_id in matched_port_locations:
                stmt = (
                    sqlite_insert(port_tbl)
                    .values(location_id=loc_id, created_at=now, updated_at=now)
                    .on_conflict_do_nothing(index_elements=["location_id"])
                )
                await conn.execute(stmt)

            # 2. Map location_id -> port_id
            res = await conn.execute(select(port_tbl.c["location_id"], port_tbl.c["id"]))
            loc_to_port_id = {row[0]: row[1] for row in res.fetchall()}
            stats.ports_created = len(loc_to_port_id)
            log.info("Total ports available in database: %d", stats.ports_created)

            # 3. Deduplicate daily stats on (port_id, stat_date)
            unique_daily: dict[tuple[int, date], float] = {}
            for loc_id, stat_date, cong_idx in daily_stats_by_loc_id:
                pid = loc_to_port_id.get(loc_id)
                if pid is not None:
                    unique_daily[(pid, stat_date)] = cong_idx

            daily_items = list(unique_daily.items())
            log.info(
                "Inserting %d daily congestion snapshots into 'port_daily_stats'...",
                len(daily_items),
            )

            exc = daily_tbl.c
            for i in range(0, len(daily_items), batch_size):
                chunk = daily_items[i : i + batch_size]
                values = [
                    {
                        "port_id": pid,
                        "stat_date": s_date,
                        "congestion_index": cong_idx,
                    }
                    for (pid, s_date), cong_idx in chunk
                ]
                stmt = (
                    sqlite_insert(daily_tbl)
                    .values(values)
                    .on_conflict_do_update(
                        index_elements=["port_id", "stat_date"],
                        set_={"congestion_index": exc["congestion_index"]},
                    )
                )
                await conn.execute(stmt)
                stats.daily_stats_inserted += len(chunk)
                if (i // batch_size) % 10 == 0 or i + batch_size >= len(daily_items):
                    log.info(
                        "Upserted daily stats [%d..%d] of %d",
                        i + 1,
                        min(i + batch_size, len(daily_items)),
                        len(daily_items),
                    )

    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# CLI and Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ingest IMF port activity and maritime performance data.",
    )
    p.add_argument(
        "--activity",
        default=None,
        help="Path to IMF Daily Port Activity CSV",
    )
    p.add_argument(
        "--performance",
        default=None,
        help="Path to Maritime Port Performance CSV",
    )
    p.add_argument(
        "--start",
        default="2024-01-01",
        help="Filter activity start date (YYYY-MM-DD, default: 2024-01-01)",
    )
    p.add_argument(
        "--end",
        default=None,
        help="Filter activity end date (YYYY-MM-DD, default: None)",
    )
    p.add_argument(
        "--match-threshold",
        type=float,
        default=0.80,
        help="Fuzzy match similarity threshold [0.0..1.0] (default: 0.80)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after N activity rows (0 = process all)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size for database upserts (default: 1000)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse, match and compute stats only; do not write to database",
    )
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    stats = ParseStats()
    start_d = _parse_date(args.start) if args.start else None
    end_d = _parse_date(args.end) if args.end else None

    # 1. Resolve input files
    act_file = get_activity_file(args.activity)
    wpi_file = get_wpi_file()

    # 2. Connect to database and load PORT locations
    db_url = get_db_url()
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            from nexafreight.models.location import Location  # type: ignore

            loc_tbl = Location.__table__
            res = await conn.execute(
                select(
                    loc_tbl.c["id"],
                    loc_tbl.c["locode"],
                    loc_tbl.c["name"],
                    loc_tbl.c["country_code"],
                ).where(loc_tbl.c["location_type"] == "PORT")
            )
            db_ports = [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in res.fetchall()]
    finally:
        await engine.dispose()

    if not db_ports:
        log.error("No PORT locations found in database. Run scripts/02_ingest_unlocode.py first.")
        return 1

    log.info("Loaded %d PORT locations from database for matching", len(db_ports))

    # 3. Load activity data
    activity_rows = _load_activity_data(act_file, start_d, end_d, args.limit, stats)
    if not activity_rows:
        log.warning("No activity records to process.")
        return 0

    # 4. Match unique port names against DB locations
    matcher = build_port_matcher(db_ports, wpi_file)
    unique_names = {r.port_identifier for r in activity_rows}
    log.info("Matching %d unique port names against database locations...", len(unique_names))

    port_name_to_loc: dict[str, tuple[int, str]] = {}
    for name in unique_names:
        matched = matcher(name, threshold=args.match_threshold)
        if matched:
            port_name_to_loc[name] = matched
            stats.matched_ports += 1
        else:
            stats.unmatched_ports += 1

    log.info(
        "Port matching results: %d matched (%.1f%%), %d unmatched",
        stats.matched_ports,
        (stats.matched_ports / len(unique_names) * 100) if unique_names else 0,
        stats.unmatched_ports,
    )

    # 5. Filter activity rows to matched ports & compute rolling congestion
    matched_activity = [r for r in activity_rows if r.port_identifier in port_name_to_loc]
    computed_activity = compute_congestion(matched_activity)

    # Collect matched location IDs and daily stats
    matched_location_ids: set[int] = {
        port_name_to_loc[r.port_identifier][0] for r in computed_activity
    }
    daily_stats_to_insert: list[tuple[int, date, float]] = [
        (port_name_to_loc[r.port_identifier][0], r.stat_date, r.congestion_index or 1.0)
        for r in computed_activity
    ]

    log.info(
        "Prepared %d daily congestion points across %d distinct ports",
        len(daily_stats_to_insert),
        len(matched_location_ids),
    )

    if args.dry_run:
        log.info("Dry-run mode enabled — no records written to database.")
        return 0

    # 6. Populate database
    await populate_database(
        matched_location_ids,
        daily_stats_to_insert,
        stats,
        batch_size=args.batch_size,
    )

    log.info(
        "Ingestion completed: ports=%d, daily_stats=%d, provenance=%s",
        stats.ports_created,
        stats.daily_stats_inserted,
        PROVENANCE,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except Exception as exc:
        log.exception("Port ingestion failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
