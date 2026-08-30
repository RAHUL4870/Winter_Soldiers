#!/usr/bin/env python3
"""
08_download_ais_history.py
==========================
T-025 (Phase 2, Data Engineer) — Downloads historical AIS data and stores it as
compact Parquet filtered to your vessel MMSIs.

DEPENDS ON: scripts/06_assign_vessels.py (T-019) — the `vessels` table must be
populated so we know which MMSIs to keep. (Or pass --mmsi / --mmsi-file / fallback to catalog).

Run order contract:
    1-7  scripts/02..07 (Phase 1 data)
    8.   scripts/08_download_ais_history.py   <-- YOU ARE HERE
    (later) scripts used by ReplayFeedAdapter when USE_LIVE_AIS=false

What it does:
-------------
1. Loads target vessel MMSIs (from `vessels` table, CLI, or `vessel_catalog.json`).
2. Downloads historical AIS for those MMSIs from NOAA / DMA, or simulates realistic
   trajectories with `SIMULATED` provenance.
3. Converts the filtered CSV into compact Snappy-compressed Parquet with pyarrow (~10x smaller).
4. Writes one Parquet file per vessel MMSI into data/raw/ais_historical/{mmsi}.parquet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pcsv
import pyarrow.parquet as pq

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
log = logging.getLogger("nexafreight.download_ais")

OUT_DEFAULT = str(_REPO_ROOT / "data" / "raw" / "ais_historical")


# --------------------------------------------------------------------------- #
# Config & Database Resolution
# --------------------------------------------------------------------------- #
def get_db_url() -> str:
    try:
        from nexafreight.config import get_settings  # type: ignore

        return get_settings().database_url
    except Exception:
        db_path = os.getenv("DATABASE_PATH", "./data/nexafreight.db")
        if (
            not db_path.startswith("./")
            and not db_path.startswith("/")
            and not db_path.startswith(":")
        ):
            db_path = f"./{db_path}"
        return f"sqlite+aiosqlite:///{db_path}"


def _resolve_vessels_table() -> Any:
    try:
        from nexafreight.models.vessel import Vessel  # type: ignore

        return Vessel.__table__
    except Exception:
        from sqlalchemy import Column, Integer, MetaData, String, Table

        meta = MetaData()
        return Table(
            "vessels",
            meta,
            Column("id", Integer, primary_key=True),
            Column("mmsi", Integer, unique=True),
            Column("name", String),
        )


async def _load_mmsis_from_db() -> list[int]:
    try:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import create_async_engine

        db_url = get_db_url()
        engine = create_async_engine(db_url)
        vessels_tbl = _resolve_vessels_table()
        async with engine.connect() as conn:
            result = await conn.execute(select(vessels_tbl.c["mmsi"]))
            return [int(r[0]) for r in result.fetchall() if r[0] is not None]
    except Exception as exc:
        log.warning("Could not read MMSIs from database (%s); checking vessel catalog...", exc)
        return []


def _load_mmsis_from_catalog() -> list[int]:
    candidates: list[str | Path] = [
        _REPO_ROOT / "data" / "raw" / "vessels" / "vessel_catalog.json",
        _REPO_ROOT.parent / "data" / "raw" / "vessels" / "vessel_catalog.json",
        _REPO_ROOT.parent.parent / "data" / "raw" / "vessels" / "vessel_catalog.json",
    ]
    for c in candidates:
        p = Path(c)
        if p.is_file():
            try:
                with open(p, encoding="utf-8") as fh:
                    catalog: dict[str, list[dict[str, Any]]] = json.load(fh)
                mmsis: list[int] = []
                for vessels in catalog.values():
                    for v in vessels:
                        if "mmsi" in v:
                            mmsis.append(int(v["mmsi"]))
                return sorted(set(mmsis))
            except Exception as exc:
                log.warning("Error parsing catalog %s: %s", p, exc)
    return [477016900, 636014307, 211281610, 353136000, 218774000, 477305900, 311000632]


def load_mmsis(mmsi_csv: str | None, mmsi_file: str | None) -> list[int]:
    if mmsi_csv:
        return [int(x.strip()) for x in mmsi_csv.split(",") if x.strip()]
    if mmsi_file:
        out = []
        for line in Path(mmsi_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(int(line))
        return out

    db_mmsis = asyncio.run(_load_mmsis_from_db())
    if db_mmsis:
        return db_mmsis
    return _load_mmsis_from_catalog()


# --------------------------------------------------------------------------- #
# 2. Download + streaming CSV filter
# --------------------------------------------------------------------------- #
def _try_resolve_mmsi_in_line(line: str, target: set[int]) -> bool:
    for tok in line.replace(",", " ").split():
        try:
            if int(float(tok)) in target:
                return True
        except (ValueError, OverflowError):
            continue
    return False


def download_and_filter(
    url: str,
    target_mmsis: set[int],
    out_csv: Path,
) -> int:
    kept = 0
    log.info("Downloading %s -> filtering for %d MMSIs", url, len(target_mmsis))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with (
        urllib.request.urlopen(url, timeout=120) as resp,  # noqa: S310
        open(out_csv, "w", newline="", encoding="utf-8") as fh,
    ):
        header = resp.readline().decode("utf-8", errors="ignore")
        fh.write(header)
        for raw in resp:
            line = raw.decode("utf-8", errors="ignore").rstrip("\n")
            if _try_resolve_mmsi_in_line(line, target_mmsis):
                fh.write(line + "\n")
                kept += 1
                if kept % 200000 == 0:
                    log.info("  ...%d matching rows so far", kept)
    log.info("Download complete: kept %d matching rows", kept)
    return kept


# --------------------------------------------------------------------------- #
# 3. Simulation mode (offline demo data)
# --------------------------------------------------------------------------- #
def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1 = math.radians(lat1)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(math.radians(lat2))
    y = math.cos(rlat1) * math.sin(math.radians(lat2)) - math.sin(rlat1) * math.cos(
        math.radians(lat2)
    ) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _advance(lat: float, lon: float, heading_deg: float, dist_km: float) -> tuple[float, float]:
    brng = math.radians(heading_deg)
    lat1 = math.radians(lat)
    d = dist_km / 6371.0
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brng))
    lon2 = lon + math.degrees(
        math.atan2(
            math.sin(brng) * math.sin(d) * math.cos(lat1),
            math.cos(d) - math.sin(lat1) * math.sin(lat2),
        )
    )
    return math.degrees(lat2), lon2


def simulate_ais(
    mmsis: list[int],
    out_csv: Path,
    days: int,
    interval_s: int,
    start: datetime | None = None,
) -> int:
    start = start or datetime.now(UTC) - timedelta(days=days)
    rng = random.Random(1234)  # noqa: S311
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    cols = [
        "mmsi",
        "timestamp",
        "lat",
        "lon",
        "speed_knots",
        "heading_deg",
        "nav_status",
        "source",
    ]

    rows = 0
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        fh.write(",".join(cols) + "\n")
        n_steps = int((days * 24 * 3600) / interval_s)
        for mmsi in mmsis:
            rng.seed(1000 + mmsi)
            lat = rng.uniform(20.0, 50.0)
            lon = rng.uniform(-75.0, 5.0)
            tgt_lat = lat + rng.uniform(5.0, 15.0) * rng.choice([-1, 1])
            tgt_lon = lon + rng.uniform(10.0, 30.0) * rng.choice([-1, 1])
            heading = _bearing_deg(lat, lon, tgt_lat, tgt_lon)
            speed = rng.uniform(12.0, 18.0)

            t = start
            for _ in range(n_steps):
                if rng.random() < 0.02:
                    speed = rng.uniform(0.0, 0.5)
                else:
                    speed = min(max(speed + rng.uniform(-0.5, 0.5), 0.0), 24.0)
                    heading = (heading + rng.uniform(-2.0, 2.0)) % 360.0

                dist_km = speed * (interval_s / 3600.0)
                lat, lon = _advance(lat, lon, heading, dist_km)
                lat = max(-90.0, min(90.0, lat))
                lon = (lon + 540.0) % 360.0 - 180.0

                fh.write(
                    f"{mmsi},{t.isoformat()},{lat:.6f},{lon:.6f},"
                    f"{speed:.1f},{heading:.1f},UNDER_WAY_USING_ENGINE,SIMULATED\n"
                )
                rows += 1
                t += timedelta(seconds=interval_s)

    log.info("Simulated %d AIS position rows for %d MMSIs", rows, len(mmsis))
    return rows


# --------------------------------------------------------------------------- #
# 4. CSV -> Parquet (pyarrow), one file per MMSI
# --------------------------------------------------------------------------- #
def csv_to_parquet_per_mmsi(csv_path: Path, out_dir: Path) -> dict[int, int]:
    table = pcsv.read_csv(csv_path)
    renamed = {c: c.strip().lower() for c in table.column_names}
    table = table.rename_columns([renamed[c] for c in table.column_names])
    table = _coerce_types(table)

    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[int, int] = {}
    mmsi_arr = table.column("mmsi").to_pylist()
    for mmsi in sorted(set(int(x) for x in mmsi_arr)):
        mask = [int(x) == mmsi for x in mmsi_arr]
        sub = table.filter(pa.array(mask))
        path = out_dir / f"{mmsi}.parquet"
        pq.write_table(sub, path, compression="snappy")
        counts[mmsi] = sub.num_rows
        log.info("Wrote %s (%d rows)", path.name, sub.num_rows)
    return counts


def _coerce_types(table: pa.Table) -> pa.Table:
    casts: dict[str, Any] = {}
    if "mmsi" in table.column_names:
        casts["mmsi"] = pa.int64()
    for name in ("lat", "lon", "speed_knots", "heading_deg"):
        if name in table.column_names:
            casts[name] = pa.float64()
    if not casts:
        return table
    return table.cast(
        pa.schema(
            [
                (n, casts[n]) if n in casts else (n, table.schema.field(n).type)
                for n in table.column_names
            ]
        )
    )


# --------------------------------------------------------------------------- #
# 5. CLI & Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download historical AIS data -> Parquet per MMSI.",
    )
    p.add_argument("--source", choices=["noaa", "dma"], default="noaa")
    p.add_argument(
        "--url",
        default=None,
        help="Direct CSV URL. If omitted, uses default source URL.",
    )
    p.add_argument(
        "--simulate",
        action="store_true",
        help="Generate synthetic AIS data (ideal for offline development & demo).",
    )
    p.add_argument("--mmsi", default=None, help="Comma-separated MMSIs (bypasses DB).")
    p.add_argument("--mmsi-file", default=None, help="File of MMSIs (one per line).")
    p.add_argument("--out", default=OUT_DEFAULT)
    p.add_argument("--days", type=int, default=7, help="Simulation window in days.")
    p.add_argument("--interval-s", type=int, default=300, help="Simulation interval in seconds.")
    return p.parse_args(argv)


_DEFAULT_URLS = {
    "noaa": "https://coast.noaa.gov/htdata/CMSP/AISDataHandler/2024/AIS_2024_01_01.csv",
    "dma": "https://www.dma.dk/aisdata/AIS_2024_01_01.zip",
}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        mmsis = load_mmsis(args.mmsi, args.mmsi_file)
        if not mmsis:
            log.error("No MMSIs to fetch. Populate vessels table or pass --mmsi.")
            return 1
        log.info("Target MMSIs (%d): %s", len(mmsis), mmsis)

        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        work_csv = out_dir / "_ais_filtered_work.csv"

        if args.simulate:
            simulate_ais(mmsis, work_csv, args.days, args.interval_s)
        else:
            url = args.url or _DEFAULT_URLS[args.source]
            if url.endswith(".zip"):
                log.warning("ZIP URL requires manual extraction: %s; falling back to simulate", url)
                simulate_ais(mmsis, work_csv, args.days, args.interval_s)
            else:
                try:
                    download_and_filter(url, set(mmsis), work_csv)
                except Exception as exc:
                    log.warning("Network download failed (%s); generating synthetic tracks", exc)
                    simulate_ais(mmsis, work_csv, args.days, args.interval_s)

        if not work_csv.exists() or work_csv.stat().st_size == 0:
            log.warning("No data produced; nothing to convert.")
            return 1

        counts = csv_to_parquet_per_mmsi(work_csv, out_dir)
        total = sum(counts.values())
        log.info(
            "Done: generated Parquet archives for %d vessels (%d total rows) in %s",
            len(counts),
            total,
            out_dir,
        )

        if work_csv.exists():
            work_csv.unlink()
        return 0
    except Exception as exc:
        log.exception("download_ais failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
