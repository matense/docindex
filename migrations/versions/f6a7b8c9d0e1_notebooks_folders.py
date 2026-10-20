"""Create the per-drive "DocIndex Notebooks" folder for existing drives.

Old notebooks keep working wherever they are (list/editor/AI/automation
look them up by id/extension, never by folder); this only ensures the
dedicated folder exists so the organize flow and new-notebook defaults
apply to pre-existing drives too. Synced (read-only) drives are skipped.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-20 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        "INSERT INTO folders (name, parent_id, user_id, drive_id, created_at) "
        "SELECT 'DocIndex Notebooks', NULL, d.user_id, d.id, CURRENT_TIMESTAMP "
        "FROM drives d "
        "WHERE d.source_path IS NULL "
        "AND d.deleted_at IS NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM folders f "
        "WHERE f.drive_id = d.id AND f.parent_id IS NULL "
        "AND f.name = 'DocIndex Notebooks')"
    ))


def downgrade():
    # Only remove auto-created folders that are still empty.
    op.execute(sa.text(
        "DELETE FROM folders "
        "WHERE name = 'DocIndex Notebooks' AND parent_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM folders c "
        "WHERE c.parent_id = folders.id) "
        "AND NOT EXISTS (SELECT 1 FROM files fl "
        "WHERE fl.folder_id = folders.id)"
    ))
