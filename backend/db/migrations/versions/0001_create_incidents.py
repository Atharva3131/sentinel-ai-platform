"""Create incidents table.

Revision ID: 0001
Revises:
Create Date: 2026-09-28 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply the migration."""
    op.create_table(
        "incidents",
        sa.Column("incident_id", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("environment", sa.String(128), nullable=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("affected_services_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("symptoms_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("tags_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("signals_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("metadata_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("incident_id", name="pk_incidents"),
    )
    op.create_index("ix_incidents_status", "incidents", ["status"])
    op.create_index("ix_incidents_correlation_id", "incidents", ["correlation_id"])


def downgrade() -> None:
    """Revert the migration."""
    op.drop_index("ix_incidents_correlation_id", table_name="incidents")
    op.drop_index("ix_incidents_status", table_name="incidents")
    op.drop_table("incidents")
