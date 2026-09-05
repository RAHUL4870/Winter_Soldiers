"""004_composite_indexes

Revision ID: 042c50783a39
Revises: 7c9635bcdd38
Create Date: 2026-09-05 10:04:08.883916+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "042c50783a39"
down_revision: str | None = "7c9635bcdd38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NF-003: Add the 5 missing composite indexes.
    with op.batch_alter_table("legs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_legs_shipment_version_seq",
            ["shipment_id", "route_version", "sequence_number"],
            unique=False,
        )
        batch_op.create_index("ix_legs_vessel_id", ["vessel_id"], unique=False)
        batch_op.create_index("ix_legs_status_mode", ["status", "transport_mode"], unique=False)

    with op.batch_alter_table("position_reports", schema=None) as batch_op:
        batch_op.create_index(
            "ix_position_reports_asset_time",
            ["asset_type", "mmsi", "reported_at"],
            unique=False,
        )

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(
            "ix_events_shipment_id_occurred_at_desc",
            ["shipment_id", "occurred_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("ix_events_shipment_id_occurred_at_desc")

    with op.batch_alter_table("position_reports", schema=None) as batch_op:
        batch_op.drop_index("ix_position_reports_asset_time")

    with op.batch_alter_table("legs", schema=None) as batch_op:
        batch_op.drop_index("ix_legs_status_mode")
        batch_op.drop_index("ix_legs_vessel_id")
        batch_op.drop_index("ix_legs_shipment_version_seq")
