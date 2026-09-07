"""AIConnection.max_prompt_tokens

Per-connection prompt budget in estimated tokens; NULL falls back to the
global AI_MAX_PROMPT_TOKENS (default 60000). Context size is a property of
the model, so the budget is per connection.

Revision ID: f8c2e5a43b60
Revises: e7b1d4f32a59
Create Date: 2026-09-07 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f8c2e5a43b60'
down_revision = 'e7b1d4f32a59'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_connections") as batch:
        batch.add_column(sa.Column("max_prompt_tokens", sa.Integer(),
                                   nullable=True))


def downgrade():
    with op.batch_alter_table("ai_connections") as batch:
        batch.drop_column("max_prompt_tokens")
