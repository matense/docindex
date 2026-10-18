"""File embeddings for native semantic search.

file_embeddings holds one L2-normalized float32 vector per text chunk
(extracted text, or image caption + name). No vector extension needed:
cosine similarity runs in Python (numpy fast path when installed).

Revision ID: 7e2a9c4f1b83
Revises: 3f9c2a1b8d44
Create Date: 2026-10-18 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7e2a9c4f1b83'
down_revision = '3f9c2a1b8d44'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "file_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_id", sa.Integer(), sa.ForeignKey("files.id"),
                  nullable=False, index=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False, default=0),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("text_hash", sa.String(64), nullable=True),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("dims", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("file_id", "chunk_index"),
    )


def downgrade():
    op.drop_table("file_embeddings")
