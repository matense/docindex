"""Favorites: Behavior.favorite + PersonaPref.favorite.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-20 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("behaviors") as batch:
        batch.add_column(sa.Column("favorite", sa.Boolean(),
                                   nullable=False, server_default=sa.false()))
    with op.batch_alter_table("persona_prefs") as batch:
        batch.add_column(sa.Column("favorite", sa.Boolean(),
                                   nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("persona_prefs") as batch:
        batch.drop_column("favorite")
    with op.batch_alter_table("behaviors") as batch:
        batch.drop_column("favorite")
