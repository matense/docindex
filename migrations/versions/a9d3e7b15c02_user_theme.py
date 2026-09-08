"""User.theme

Per-user UI theme preference ("light" default / "dark"). Applied via the
<html data-theme="..."> attribute, which switches the DaisyUI palette; custom
components follow through the [data-theme="dark"] overrides in style.css.

Revision ID: a9d3e7b15c02
Revises: f8c2e5a43b60
Create Date: 2026-09-09 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9d3e7b15c02'
down_revision = 'f8c2e5a43b60'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("theme", sa.String(length=10),
                                   nullable=False, server_default="light"))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("theme")
