"""003_add_event_reroute_option

Revision ID: 7c9635bcdd38
Revises: 002_composite_index
Create Date: 2026-09-05 06:17:34.926956+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c9635bcdd38"
down_revision: str | None = "002_composite_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NF-002: Add events and reroute_options tables only.
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shipment_id", sa.String(length=36), nullable=False),
        sa.Column("leg_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location_locode", sa.String(length=50), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["leg_id"],
            ["legs.id"],
            name=op.f("fk_events_leg_id_legs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_events_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )

    op.create_table(
        "reroute_options",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_id", sa.String(length=36), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("eta", sa.String(length=50), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name=op.f("fk_reroute_options_alert_id_alerts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reroute_options")),
    )
    with op.batch_alter_table("reroute_options", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reroute_options_alert_id"), ["alert_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("reroute_options", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_reroute_options_alert_id"))

    op.drop_table("reroute_options")
    op.drop_table("events")
