"""AIConnection.embedding_model for per-connection semantic search.

Empty (default) keeps the previous behavior: the global AI_EMBEDDING_MODEL
is used. NULLs from older rows behave like empty.

Revision ID: 9a1b2c3d4e55
Revises: 7e2a9c4f1b83
Create Date: 2026-10-18 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a1b2c3d4e55'
down_revision = '7e2a9c4f1b83'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_connections") as batch:
        batch.add_column(sa.Column("embedding_model", sa.String(120),
                                   nullable=False, server_default=""))


def downgrade():
    with op.batch_alter_table("ai_connections") as batch:
        batch.drop_column("embedding_model")
