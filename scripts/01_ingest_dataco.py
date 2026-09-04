#!/usr/bin/env python3
"""
01_ingest_dataco.py
===================
T-016 (Phase 1, Data Engineer) — Ingests the DataCo Smart Supply Chain dataset
into the `orders` and `order_items` tables.

DEPENDS ON: scripts/02_ingest_unlocode.py (T-014) & scripts/03_ingest_port_data.py (T-015).

Run order contract:
    1.  scripts/02_ingest_unlocode.py
    2.  scripts/03_ingest_port_data.py
    3.  scripts/01_ingest_dataco.py   <-- YOU ARE HERE
    4.  scripts/04_consolidate_shipments.py
    ...

What it does
------------
1. Reads DataCoSupplyChainDataset.csv with pandas.
2. Maps `Shipping Mode` -> primary shipping_mode:
        Standard Class -> SEA
        Second Class   -> RAIL
        First Class    -> AIR
        Same Day       -> AIR
3. Computes `sla_deadline = order_date + days_for_shipment_scheduled`.
4. Maps `Category Name` -> cargo_class (STANDARD / REFRIGERATED / HAZMAT / HIGH_VALUE).
5. Aggregates multi-item orders:
   - Sums item revenues into `orders.revenue`
   - Estimates shipping cost
   - Assigns `historical_late_delivery` flag from `Late_delivery_risk`
6. Bulk-inserts / upserts 65,752 `orders` and 180,519 `order_items` idempotently on `order_number`.

Usage
-----
    python scripts/01_ingest_dataco.py
    python scripts/01_ingest_dataco.py --input data/raw/dataco/DataCoSupplyChainDataset.csv
    python scripts/01_ingest_dataco.py --limit 1000 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore
from sqlalchemy import (
    Boolean,
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
log = logging.getLogger("nexafreight.ingest_dataco")

PROVENANCE = "DERIVED"
SOURCE = "DATACO_CSV"


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
# File resolution
# --------------------------------------------------------------------------- #
def resolve_dataco_file(user_path: str | None) -> Path:
    candidates: list[str | Path] = [
        user_path or "",
        _REPO_ROOT / "data" / "raw" / "dataco" / "DataCoSupplyChainDataset.csv",
        _REPO_ROOT.parent.parent
        / "Datasets"
        / "Primary Dataset — DataCo Smart Supply Chain"
        / "DataCoSupplyChainDataset.csv",
        _REPO_ROOT.parent
        / "Datasets"
        / "Primary Dataset — DataCo Smart Supply Chain"
        / "DataCoSupplyChainDataset.csv",
        _REPO_ROOT
        / "Datasets"
        / "Primary Dataset — DataCo Smart Supply Chain"
        / "DataCoSupplyChainDataset.csv",
    ]
    for cand in candidates:
        p = Path(cand)
        if p.is_file():
            return p
        if p.is_dir():
            csvs = list(p.glob("*.csv"))
            if csvs:
                return csvs[0]
    raise FileNotFoundError(f"Could not locate DataCo CSV at candidates: {candidates}")


# --------------------------------------------------------------------------- #
# Mapping Helpers
# --------------------------------------------------------------------------- #
def map_shipping_mode(mode_str: str) -> str:
    """Map DataCo Shipping Mode to domain TransportMode (SEA, RAIL, AIR, ROAD)."""
    m = str(mode_str or "").strip().lower()
    if "standard" in m:
        return "SEA"
    if "second" in m:
        return "RAIL"
    if "first" in m or "same" in m:
        return "AIR"
    return "SEA"


def map_cargo_class(category: str) -> str:
    """Map Product Category Name to domain CargoClass."""
    c = str(category or "").strip().lower()
    if any(k in c for k in ("clean", "chem", "paint", "battery", "hazard", "solvent", "flamm")):
        return "HAZMAT"
    if any(
        k in c
        for k in ("meat", "fruit", "veg", "dairy", "bake", "frozen", "fish", "food", "perish")
    ):
        return "REFRIGERATED"
    if any(k in c for k in ("computer", "camera", "electron", "phone", "audio", "luxur", "jewel")):
        return "HIGH_VALUE"
    return "STANDARD"


# --------------------------------------------------------------------------- #
# Database Tables & Persistence
# --------------------------------------------------------------------------- #
def _resolve_tables():
    try:
        from nexafreight.models.order import Order, OrderItem  # type: ignore

        return Order.__table__, OrderItem.__table__
    except Exception:
        meta = MetaData()
        orders_tbl = Table(
            "orders",
            meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("order_number", String(50), unique=True, nullable=False, index=True),
            Column(
                "shipment_id",
                String(36),
                ForeignKey("shipments.id", ondelete="SET NULL"),
                nullable=True,
                index=True,
            ),
            Column("order_date", DateTime(timezone=True), nullable=False, index=True),
            Column("sla_deadline", DateTime(timezone=True), nullable=False),
            Column("revenue", Float, nullable=False),
            Column("shipping_cost", Float, nullable=False),
            Column("sla_status", String(20), nullable=False, default="ON_TIME"),
            Column("shipping_mode", String(20), nullable=False),
            Column("cargo_class", String(20), nullable=False),
            Column("historical_late_delivery", Boolean, nullable=True),
            Column("real_shipping_days", Float, nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        order_items_tbl = Table(
            "order_items",
            meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column(
                "order_id", Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
            ),
            Column("product_category", String(100), nullable=False),
            Column("quantity", Integer, nullable=False),
            Column("unit_price", Float, nullable=False),
        )
        return orders_tbl, order_items_tbl


def _set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout=30000;")
    finally:
        cursor.close()


async def persist_dataco(
    orders_data: list[dict[str, Any]],
    order_items_data: list[dict[str, Any]],
    batch_size: int = 1000,
) -> None:
    db_url = get_db_url()
    log.info("Connecting to database: %s", db_url)
    engine = create_async_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
    orders_tbl, order_items_tbl = _resolve_tables()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(orders_tbl.create, checkfirst=True)
            await conn.run_sync(order_items_tbl.create, checkfirst=True)

            now = datetime.now(UTC)

            # 1. Upsert Orders in batches
            log.info("Upserting %d orders into 'orders' table...", len(orders_data))
            exc = orders_tbl.c
            for i in range(0, len(orders_data), batch_size):
                chunk = orders_data[i : i + batch_size]
                values = [
                    {
                        "order_number": o["order_number"],
                        "shipment_id": None,
                        "order_date": o["order_date"],
                        "sla_deadline": o["sla_deadline"],
                        "revenue": round(o["revenue"], 2),
                        "shipping_cost": round(o["shipping_cost"], 2),
                        "sla_status": o["sla_status"],
                        "shipping_mode": o["shipping_mode"],
                        "cargo_class": o["cargo_class"],
                        "historical_late_delivery": o["historical_late_delivery"],
                        "real_shipping_days": o.get("real_shipping_days", 0.0),
                        "created_at": now,
                        "updated_at": now,
                    }
                    for o in chunk
                ]
                stmt = (
                    sqlite_insert(orders_tbl)
                    .values(values)
                    .on_conflict_do_update(
                        index_elements=["order_number"],
                        set_={
                            "order_date": exc["order_date"],
                            "sla_deadline": exc["sla_deadline"],
                            "revenue": exc["revenue"],
                            "shipping_cost": exc["shipping_cost"],
                            "sla_status": exc["sla_status"],
                            "shipping_mode": exc["shipping_mode"],
                            "cargo_class": exc["cargo_class"],
                            "historical_late_delivery": exc["historical_late_delivery"],
                            "real_shipping_days": exc["real_shipping_days"],
                            "updated_at": now,
                        },
                    )
                )
                await conn.execute(stmt)
                if (i // batch_size) % 10 == 0 or i + batch_size >= len(orders_data):
                    log.info(
                        "Upserted orders [%d..%d] of %d",
                        i + 1,
                        min(i + batch_size, len(orders_data)),
                        len(orders_data),
                    )

            # 2. Retrieve order_number -> orders.id map
            log.info("Querying orders table to resolve order IDs for items...")
            res = await conn.execute(select(orders_tbl.c["order_number"], orders_tbl.c["id"]))
            order_num_to_id = {row[0]: row[1] for row in res.fetchall()}
            log.info("Resolved %d order IDs in database", len(order_num_to_id))

            # 3. Clear existing order_items for affected orders (idempotent rebuild)
            # and insert new items in batches
            affected_order_ids = [
                order_num_to_id[o["order_number"]]
                for o in orders_data
                if o["order_number"] in order_num_to_id
            ]
            if affected_order_ids:
                for i in range(0, len(affected_order_ids), batch_size):
                    sub_ids = affected_order_ids[i : i + batch_size]
                    await conn.execute(
                        order_items_tbl.delete().where(order_items_tbl.c["order_id"].in_(sub_ids))
                    )

            log.info("Inserting %d order items into 'order_items' table...", len(order_items_data))
            item_values = []
            for item in order_items_data:
                oid = order_num_to_id.get(item["order_number"])
                if oid is None:
                    continue
                item_values.append(
                    {
                        "order_id": oid,
                        "product_category": item["product_category"],
                        "quantity": item["quantity"],
                        "unit_price": round(item["unit_price"], 2),
                    }
                )

            for i in range(0, len(item_values), batch_size):
                chunk = item_values[i : i + batch_size]
                await conn.execute(order_items_tbl.insert().values(chunk))
                if (i // batch_size) % 20 == 0 or i + batch_size >= len(item_values):
                    log.info(
                        "Inserted items [%d..%d] of %d",
                        i + 1,
                        min(i + batch_size, len(item_values)),
                        len(item_values),
                    )

    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- #
# CLI and Main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Ingest DataCo Smart Supply Chain dataset into orders/order_items.",
    )
    p.add_argument(
        "--input",
        default=None,
        help="Path to DataCo CSV file (default: auto-detected)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after N CSV rows (0 = process all). Useful for smoke testing.",
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
        help="Parse and validate only; do not write to the database.",
    )
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    csv_file = resolve_dataco_file(args.input)
    log.info("Loading DataCo dataset from: %s", csv_file)

    usecols = [
        "Order Id",
        "Order Item Id",
        "Order Item Quantity",
        "Order Item Product Price",
        "Order Item Total",
        "Category Name",
        "Shipping Mode",
        "Days for shipment (scheduled)",
        "Days for shipping (real)",
        "order date (DateOrders)",
        "Late_delivery_risk",
    ]

    df = pd.read_csv(csv_file, usecols=usecols, encoding="latin-1", low_memory=False)
    log.info("Loaded DataCo CSV with %d rows", len(df))

    if args.limit > 0:
        df = df.head(args.limit)
        log.info("Limited to %d rows for testing", args.limit)

    orders_dict: dict[str, dict[str, Any]] = {}
    order_items_list: list[dict[str, Any]] = []

    for _, r in df.iterrows():
        raw_oid = str(r["Order Id"]).strip()
        if not raw_oid or raw_oid.lower() == "nan":
            continue

        order_num = f"ORD-{raw_oid}"
        qty = int(r["Order Item Quantity"] or 1)
        price = float(r["Order Item Product Price"] or 0.0)
        total_val = float(r["Order Item Total"] or (qty * price))
        cat = str(r["Category Name"] or "General")
        c_class = map_cargo_class(cat)

        if order_num not in orders_dict:
            o_date_str = str(r["order date (DateOrders)"]).strip()
            try:
                o_date = datetime.strptime(o_date_str, "%m/%d/%Y %H:%M").replace(tzinfo=UTC)
            except Exception:
                o_date = datetime.now(UTC)

            try:
                days_sched = int(float(r["Days for shipment (scheduled)"] or 3))
            except (ValueError, TypeError):
                days_sched = 3

            try:
                days_real = float(r.get("Days for shipping (real)", 0) or 0)
            except (ValueError, TypeError):
                days_real = float(days_sched)

            deadline = o_date + timedelta(days=days_sched)
            mode = map_shipping_mode(str(r["Shipping Mode"]))
            try:
                late_risk = bool(int(float(r["Late_delivery_risk"] or 0)) == 1)
            except (ValueError, TypeError):
                late_risk = False

            orders_dict[order_num] = {
                "order_number": order_num,
                "order_date": o_date,
                "sla_deadline": deadline,
                "revenue": total_val,
                "shipping_cost": round(total_val * 0.12, 2),
                "sla_status": "LATE" if late_risk else "ON_TIME",
                "shipping_mode": mode,
                "cargo_class": c_class,
                "historical_late_delivery": late_risk,
                "real_shipping_days": days_real,
            }
        else:
            orders_dict[order_num]["revenue"] += total_val
            orders_dict[order_num]["shipping_cost"] += round(total_val * 0.12, 2)
            # Escalate cargo classification if needed
            curr_class = orders_dict[order_num]["cargo_class"]
            if c_class == "HAZMAT":
                orders_dict[order_num]["cargo_class"] = "HAZMAT"
            elif c_class == "REFRIGERATED" and curr_class != "HAZMAT":
                orders_dict[order_num]["cargo_class"] = "REFRIGERATED"
            elif c_class == "HIGH_VALUE" and curr_class == "STANDARD":
                orders_dict[order_num]["cargo_class"] = "HIGH_VALUE"

        order_items_list.append(
            {
                "order_number": order_num,
                "product_category": cat[:100],
                "quantity": qty,
                "unit_price": price,
            }
        )

    orders_list = list(orders_dict.values())
    log.info(
        "Aggregation summary: processed %d lines into %d unique orders with %d items",
        len(df),
        len(orders_list),
        len(order_items_list),
    )

    if args.dry_run:
        log.info("Dry-run mode enabled — no records written to database.")
        return 0

    if not orders_list:
        log.warning("No orders to persist.")
        return 0

    await persist_dataco(orders_list, order_items_list, batch_size=args.batch_size)
    log.info(
        "DataCo ingestion complete: %d orders and %d items written (provenance=%s, source=%s)",
        len(orders_list),
        len(order_items_list),
        PROVENANCE,
        SOURCE,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(amain(args))
    except Exception as exc:
        log.exception("DataCo ingestion failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
