"""Git-backed drives: Drive source fields (gitdrives module).

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd0e1f2a3b4c5'
down_revision = 'c9d0e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("drives") as batch:
        batch.add_column(sa.Column("source_kind", sa.String(20),
                                   nullable=False, server_default="folder"))
        batch.add_column(sa.Column("repo_url", sa.String(2000), nullable=True))
        batch.add_column(sa.Column("repo_branch", sa.String(255),
                                   nullable=True))
        batch.add_column(sa.Column("repo_username", sa.String(255),
                                   nullable=True))
        batch.add_column(sa.Column("repo_token", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("drives") as batch:
        batch.drop_column("repo_token")
        batch.drop_column("repo_username")
        batch.drop_column("repo_branch")
        batch.drop_column("repo_url")
        batch.drop_column("source_kind")
