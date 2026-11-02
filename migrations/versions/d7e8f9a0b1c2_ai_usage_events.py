"""ai usage events

Revision ID: d7e8f9a0b1c2
Revises: c4d5e6f7a8b9
Create Date: 2026-10-30 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7e8f9a0b1c2'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'ai_usage_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('task_id', sa.Integer(), nullable=True),
        sa.Column('message_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=False),
        sa.Column('completion_tokens', sa.Integer(), nullable=False),
        sa.Column('estimated', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['conversation_id'], ['chat_conversations.id']),
        sa.ForeignKeyConstraint(['message_id'], ['chat_messages.id']),
        sa.ForeignKeyConstraint(['task_id'], ['ai_tasks.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('ai_usage_events', schema=None) as batch_op:
        batch_op.create_index('ix_ai_usage_events_conversation_id',
                              ['conversation_id'], unique=False)
        batch_op.create_index('ix_ai_usage_events_created_at',
                              ['created_at'], unique=False)
        batch_op.create_index('ix_ai_usage_events_message_id',
                              ['message_id'], unique=False)
        batch_op.create_index('ix_ai_usage_events_task_id',
                              ['task_id'], unique=False)
        batch_op.create_index('ix_ai_usage_events_user_id',
                              ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('ai_usage_events', schema=None) as batch_op:
        batch_op.drop_index('ix_ai_usage_events_user_id')
        batch_op.drop_index('ix_ai_usage_events_task_id')
        batch_op.drop_index('ix_ai_usage_events_message_id')
        batch_op.drop_index('ix_ai_usage_events_created_at')
        batch_op.drop_index('ix_ai_usage_events_conversation_id')
    op.drop_table('ai_usage_events')
