"""Initial schema with all domain models

Revision ID: 001_initial_schema
Revises:
Create Date: 2024-01-15 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply migration: create all tables in dependency order."""
    # 1. Users (no FK dependencies)
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    # 2. Locations (no FK dependencies)
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("locode", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("location_type", sa.String(length=20), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_locations")),
        sa.UniqueConstraint("locode", name=op.f("uq_locations_locode")),
    )
    op.create_index(op.f("ix_locations_locode"), "locations", ["locode"], unique=True)

    # 3. Vessels (no FK dependencies)
    op.create_table(
        "vessels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mmsi", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("call_sign", sa.String(length=20), nullable=True),
        sa.Column("typical_lanes_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vessels")),
        sa.UniqueConstraint("mmsi", name=op.f("uq_vessels_mmsi")),
    )
    op.create_index(op.f("ix_vessels_mmsi"), "vessels", ["mmsi"], unique=True)

    # 4. Corridor Alternatives (no FK dependencies)
    op.create_table(
        "corridor_alternatives",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("option_key", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("applicable_disruption_types_json", sa.Text(), nullable=False),
        sa.Column("route_template_json", sa.Text(), nullable=False),
        sa.Column("cost_delta_factor", sa.Float(), nullable=False),
        sa.Column("time_delta_hours", sa.Float(), nullable=False),
        sa.Column("co2_delta_factor", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corridor_alternatives")),
        sa.UniqueConstraint("option_key", name=op.f("uq_corridor_alternatives_option_key")),
    )
    op.create_index(
        op.f("ix_corridor_alternatives_option_key"),
        "corridor_alternatives",
        ["option_key"],
        unique=True,
    )

    # 5. Ports (depends on locations)
    op.create_table(
        "ports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["locations.id"],
            name=op.f("fk_ports_location_id_locations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ports")),
        sa.UniqueConstraint("location_id", name=op.f("uq_ports_location_id")),
    )

    # 6. Shipments (depends on locations and self)
    op.create_table(
        "shipments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("origin_id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("primary_transport_mode", sa.String(length=20), nullable=False),
        sa.Column("cargo_class", sa.String(length=20), nullable=False),
        sa.Column("container_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("route_version", sa.Integer(), nullable=False),
        sa.Column("strictest_sla_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parent_shipment_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["locations.id"],
            name=op.f("fk_shipments_destination_id_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["origin_id"],
            ["locations.id"],
            name=op.f("fk_shipments_origin_id_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_shipment_id"],
            ["shipments.id"],
            name=op.f("fk_shipments_parent_shipment_id_shipments"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipments")),
    )
    op.create_index(op.f("ix_shipments_status"), "shipments", ["status"], unique=False)

    # 7. Port Daily Stats (depends on ports)
    op.create_table(
        "port_daily_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("port_id", sa.Integer(), nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("congestion_index", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["port_id"],
            ["ports.id"],
            name=op.f("fk_port_daily_stats_port_id_ports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_port_daily_stats")),
        sa.UniqueConstraint(
            "port_id",
            "stat_date",
            name=op.f("uq_port_daily_stats_port_id_stat_date"),
        ),
    )
    op.create_index(
        op.f("ix_port_daily_stats_stat_date"),
        "port_daily_stats",
        ["stat_date"],
        unique=False,
    )

    # 8. Orders (depends on shipments)
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_number", sa.String(length=50), nullable=False),
        sa.Column("shipment_id", sa.String(length=36), nullable=True),
        sa.Column("sla_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revenue", sa.Float(), nullable=False),
        sa.Column("shipping_cost", sa.Float(), nullable=False),
        sa.Column("sla_status", sa.String(length=20), nullable=False),
        sa.Column("shipping_mode", sa.String(length=20), nullable=False),
        sa.Column("cargo_class", sa.String(length=20), nullable=False),
        sa.Column("historical_late_delivery", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_orders_shipment_id_shipments"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("order_number", name=op.f("uq_orders_order_number")),
    )
    op.create_index(op.f("ix_orders_order_number"), "orders", ["order_number"], unique=True)
    op.create_index(op.f("ix_orders_shipment_id"), "orders", ["shipment_id"], unique=False)

    # 9. Order Items (depends on orders)
    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("product_category", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_items_order_id_orders"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )

    # 10. Legs (depends on shipments, locations, vessels)
    op.create_table(
        "legs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shipment_id", sa.String(length=36), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("route_version", sa.Integer(), nullable=False),
        sa.Column("transport_mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("origin_id", sa.Integer(), nullable=False),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("vessel_id", sa.Integer(), nullable=True),
        sa.Column("flight_number", sa.String(length=20), nullable=True),
        sa.Column("planned_departure", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_arrival", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_departure", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_arrival", sa.DateTime(timezone=True), nullable=True),
        sa.Column("route_geometry_json", sa.Text(), nullable=True),
        sa.Column("distance_km", sa.Float(), nullable=True),
        sa.Column("co2_kg", sa.Float(), nullable=True),
        sa.Column("provenance", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["locations.id"],
            name=op.f("fk_legs_destination_id_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["origin_id"],
            ["locations.id"],
            name=op.f("fk_legs_origin_id_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_legs_shipment_id_shipments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["vessel_id"],
            ["vessels.id"],
            name=op.f("fk_legs_vessel_id_vessels"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legs")),
        comment="Route segments with zero-loss rerouting (REPLACED status, never deleted)",
    )
    op.create_index(op.f("ix_legs_shipment_id"), "legs", ["shipment_id"], unique=False)

    # 11. Position Reports (depends on legs)
    op.create_table(
        "position_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("leg_id", sa.Integer(), nullable=False),
        sa.Column("asset_type", sa.String(length=20), nullable=False),
        sa.Column("mmsi", sa.Integer(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("heading", sa.Float(), nullable=True),
        sa.Column("speed_knots", sa.Float(), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["leg_id"],
            ["legs.id"],
            name=op.f("fk_position_reports_leg_id_legs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_reports")),
        comment="High-volume position tracking with mandatory provenance",
    )
    op.create_index(
        op.f("ix_position_reports_leg_id"),
        "position_reports",
        ["leg_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_position_reports_reported_at"),
        "position_reports",
        ["reported_at"],
        unique=False,
    )

    # 12. Disruptions (depends on shipments, legs)
    op.create_table(
        "disruptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("shipment_id", sa.String(length=36), nullable=False),
        sa.Column("leg_id", sa.Integer(), nullable=True),
        sa.Column("disruption_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["leg_id"],
            ["legs.id"],
            name=op.f("fk_disruptions_leg_id_legs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_disruptions_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_disruptions")),
        sa.UniqueConstraint(
            "shipment_id",
            "leg_id",
            "disruption_type",
            "status",
            name=op.f("uq_disruptions_active_per_shipment_leg_type"),
        ),
    )
    op.create_index(
        op.f("ix_disruptions_shipment_id"),
        "disruptions",
        ["shipment_id"],
        unique=False,
    )

    # 13. Alerts (depends on disruptions, shipments, users)
    op.create_table(
        "alerts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("disruption_id", sa.String(length=36), nullable=False),
        sa.Column("shipment_id", sa.String(length=36), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("financial_exposure", sa.Float(), nullable=False),
        sa.Column("sla_breach_details_json", sa.Text(), nullable=True),
        sa.Column("acknowledged_by", sa.Integer(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["acknowledged_by"],
            ["users.id"],
            name=op.f("fk_alerts_acknowledged_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["disruption_id"],
            ["disruptions.id"],
            name=op.f("fk_alerts_disruption_id_disruptions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_alerts_shipment_id_shipments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
        sa.UniqueConstraint("disruption_id", name=op.f("uq_alerts_disruption_id")),
    )
    op.create_index(op.f("ix_alerts_severity"), "alerts", ["severity"], unique=False)
    op.create_index(op.f("ix_alerts_shipment_id"), "alerts", ["shipment_id"], unique=False)

    # 14. Decisions (depends on alerts, shipments, users)
    op.create_table(
        "decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("alert_id", sa.String(length=36), nullable=False),
        sa.Column("shipment_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("chosen_option_key", sa.String(length=50), nullable=True),
        sa.Column("options_snapshot_json", sa.Text(), nullable=False),
        sa.Column("financial_impact", sa.Float(), nullable=False),
        sa.Column("route_version_before", sa.Integer(), nullable=False),
        sa.Column("route_version_after", sa.Integer(), nullable=False),
        sa.Column("approved_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name=op.f("fk_decisions_alert_id_alerts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name=op.f("fk_decisions_approved_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_decisions_shipment_id_shipments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decisions")),
        sa.UniqueConstraint("alert_id", name=op.f("uq_decisions_alert_id")),
    )
    op.create_index(op.f("ix_decisions_shipment_id"), "decisions", ["shipment_id"], unique=False)

    # 15. Audit Logs (depends on users)
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=True),
        sa.Column("entity_id", sa.String(length=100), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=True),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_audit_logs_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(op.f("ix_audit_logs_created_at"), "audit_logs", ["created_at"], unique=False)


def downgrade() -> None:
    """Revert migration: drop all tables in reverse dependency order."""
    # 1. Audit Logs
    op.drop_index(op.f("ix_audit_logs_created_at"), table_name="audit_logs")
    op.drop_table("audit_logs")

    # 2. Decisions
    op.drop_index(op.f("ix_decisions_shipment_id"), table_name="decisions")
    op.drop_table("decisions")

    # 3. Alerts
    op.drop_index(op.f("ix_alerts_shipment_id"), table_name="alerts")
    op.drop_index(op.f("ix_alerts_severity"), table_name="alerts")
    op.drop_table("alerts")

    # 4. Disruptions
    op.drop_index(op.f("ix_disruptions_shipment_id"), table_name="disruptions")
    op.drop_table("disruptions")

    # 5. Position Reports
    op.drop_index(op.f("ix_position_reports_reported_at"), table_name="position_reports")
    op.drop_index(op.f("ix_position_reports_leg_id"), table_name="position_reports")
    op.drop_table("position_reports")

    # 6. Legs
    op.drop_index(op.f("ix_legs_shipment_id"), table_name="legs")
    op.drop_table("legs")

    # 7. Order Items
    op.drop_table("order_items")

    # 8. Orders
    op.drop_index(op.f("ix_orders_shipment_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_order_number"), table_name="orders")
    op.drop_table("orders")

    # 9. Port Daily Stats
    op.drop_index(op.f("ix_port_daily_stats_stat_date"), table_name="port_daily_stats")
    op.drop_table("port_daily_stats")

    # 10. Shipments
    op.drop_index(op.f("ix_shipments_status"), table_name="shipments")
    op.drop_table("shipments")

    # 11. Ports
    op.drop_table("ports")

    # 12. Corridor Alternatives
    op.drop_index(
        op.f("ix_corridor_alternatives_option_key"),
        table_name="corridor_alternatives",
    )
    op.drop_table("corridor_alternatives")

    # 13. Vessels
    op.drop_index(op.f("ix_vessels_mmsi"), table_name="vessels")
    op.drop_table("vessels")

    # 14. Locations
    op.drop_index(op.f("ix_locations_locode"), table_name="locations")
    op.drop_table("locations")

    # 15. Users
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
