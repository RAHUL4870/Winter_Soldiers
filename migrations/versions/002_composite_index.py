"""add composite index on shipments status and mode

Revision ID: 002_composite_index
Revises: 001_initial_schema
Create Date: 2024-01-15 22:00:00.000000

"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_composite_index"
down_revision: str | None = "001_initial_schema"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add composite index for efficient status+mode filtering."""
    op.create_index(
        "ix_shipments_status_mode", "shipments", ["status", "primary_transport_mode"], unique=False
    )


def downgrade() -> None:
    """Remove composite index."""
    op.drop_index("ix_shipments_status_mode", table_name="shipments")
