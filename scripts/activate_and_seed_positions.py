"""Script to activate demo shipments and seed initial interpolated positions."""

from __future__ import annotations

import asyncio
import sqlite3

from nexafreight.database import get_session_factory
from nexafreight.workers.position_interpolator import _run_interpolation_job


def activate_legs() -> None:
    con = sqlite3.connect("./data/nexafreight.db")
    cur = con.cursor()
    cur.execute(
        "UPDATE shipments SET status = 'IN_TRANSIT' "
        "WHERE id IN (SELECT id FROM shipments LIMIT 500);"
    )
    cur.execute(
        "UPDATE legs SET status = 'IN_PROGRESS', actual_departure = datetime('now', '-30 minutes') "
        "WHERE shipment_id IN (SELECT id FROM shipments WHERE status = 'IN_TRANSIT') "
        "AND sequence_number IN (1, 2);"
    )
    con.commit()
    in_prog = cur.execute("SELECT COUNT(*) FROM legs WHERE status = 'IN_PROGRESS';").fetchone()[0]
    print(f"Active IN_PROGRESS legs: {in_prog}")
    con.close()


async def run_worker() -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        await _run_interpolation_job(session)


def check_positions() -> None:
    con = sqlite3.connect("./data/nexafreight.db")
    cur = con.cursor()
    count = cur.execute("SELECT COUNT(*) FROM position_reports;").fetchone()[0]
    print(f"Total position reports in database: {count}")
    cur.execute(
        "SELECT leg_id, asset_type, latitude, longitude, provenance, reported_at "
        "FROM position_reports LIMIT 5;"
    )
    for row in cur.fetchall():
        print("  Sample position:", row)
    con.close()


if __name__ == "__main__":
    activate_legs()
    asyncio.run(run_worker())
    check_positions()
