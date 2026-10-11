"""Core behaviors & persona preferences

Behaviors (reusable AI instructions) become a core feature instead of
living in the Automation module: new ``behaviors`` table (with an
``enabled`` flag so users can trim their pickers) and ``persona_prefs``
(per-user enable/disable for the bundled Agency personas). Existing rows
from the module's ``mod_automation_behavior`` table are copied over; the
old table is left in place untouched.

Revision ID: c1a2b3d4e5f6
Revises: a2d8f4c17e90
Create Date: 2026-10-10 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1a2b3d4e5f6'
down_revision = 'a2d8f4c17e90'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "behaviors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "persona_prefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"),
                  nullable=False, index=True),
        sa.Column("persona_key", sa.String(length=160), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.UniqueConstraint("user_id", "persona_key",
                            name="uq_persona_pref_user_key"),
    )
    # Carry over behaviors created while the feature lived in the
    # Automation module (older databases only).
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "mod_automation_behavior" in tables:
        bind.execute(sa.text(
            "INSERT INTO behaviors (user_id, name, content, enabled,"
            " created_at, updated_at)"
            " SELECT user_id, name, content, 1, created_at, updated_at"
            " FROM mod_automation_behavior"))


def downgrade():
    op.drop_table("persona_prefs")
    op.drop_table("behaviors")
