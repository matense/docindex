"""Git Sync per-drive commit identity and remote credentials."""

import sqlalchemy as sa
from alembic import op

revision = "gitsync_0002"
down_revision = "gitsync_0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("mod_gitsync_repository") as batch:
        batch.add_column(sa.Column("author_name", sa.String(120), nullable=False, server_default=""))
        batch.add_column(sa.Column("author_email", sa.String(254), nullable=False, server_default=""))
        batch.add_column(sa.Column("remote_username", sa.String(255), nullable=True))
        batch.add_column(sa.Column("remote_token", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("mod_gitsync_repository") as batch:
        batch.drop_column("remote_token")
        batch.drop_column("remote_username")
        batch.drop_column("author_email")
        batch.drop_column("author_name")
