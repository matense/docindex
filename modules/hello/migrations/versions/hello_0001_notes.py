"""hello module: mod_hello_note table

Independent alembic root for the hello example module — module migrations do
not join the core chain so removing the module never breaks core upgrades.

Revision ID: hello_0001
Revises:
Create Date: 2026-09-13 10:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'hello_0001'
down_revision = None
branch_labels = ('hello',)
depends_on = None


def upgrade():
    op.create_table(
        "mod_hello_note",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("mod_hello_note")
