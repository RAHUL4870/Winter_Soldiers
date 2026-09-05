"""add_port_stats_vessel_count_rolling_avg

Revision ID: fdad75e4411d
Revises: 042c50783a39
Create Date: 2026-09-05 10:27:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fdad75e4411d'
down_revision: Union[str, None] = '042c50783a39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add vessel_count and rolling_90_day_avg to port_daily_stats
    op.add_column('port_daily_stats', sa.Column('vessel_count', sa.Integer(), nullable=True))
    op.add_column('port_daily_stats', sa.Column('rolling_90_day_avg', sa.Float(), nullable=True))


def downgrade() -> None:
    # SQLite does not easily support drop_column, but we define it for completeness
    with op.batch_alter_table('port_daily_stats', schema=None) as batch_op:
        batch_op.drop_column('rolling_90_day_avg')
        batch_op.drop_column('vessel_count')
