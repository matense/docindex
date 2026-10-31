"""Split the search index into its own database file.

Revision ID: f1e2d3c4b5a6
Revises: d7e8f9a0b1c2
Create Date: 2026-10-31 00:00:00.000000

Moves the search-index tables (`file_index`, `file_embeddings`, `file_fts*`)
from the main database into a sibling `<stem>_index.sqlite` file, attached
as `idx` on every connection (see `_sqlite_listener` in app/__init__.py).
The ORM models carry `__table_args__ = {"schema": "idx"}`, so all Python
access is unchanged.

Why: SQLite has a single writer per file - index drainers used to contend
with interactive writes ("database is locked"). After this split, index
writes only contend with each other (additionally serialized in-process
via `app.services.db_locks`), and the index file is fully derivable from
the stored files (drop + reindex to rebuild).

The full FTS payload is copied through the FTS5 API (INSERT..SELECT),
never by copying shadow tables, so the new index is consistent by
construction. Counts are verified before the old tables are dropped.
"""

import os
import time

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f1e2d3c4b5a6'
down_revision = 'd7e8f9a0b1c2'
branch_labels = None
depends_on = None


def _core_path():
    from sqlalchemy.engine.url import make_url
    url = op.get_bind().engine.url
    if url.get_backend_name() != "sqlite":
        return None
    db_path = make_url(str(url)).database
    if not db_path or db_path == ":memory:":
        return None
    return db_path


def _index_path(core_path):
    root, ext = os.path.splitext(core_path)
    return root + "_index" + (ext or ".sqlite")


def _attach(bind, index_path):
    # The migration connection may already carry `idx` (the app engine's
    # connect listener attaches it) - ATTACH-ing twice errors out.
    names = [row[1] for row in bind.execute(sa.text("PRAGMA database_list"))]
    if "idx" in names:
        return
    quoted = "'" + index_path.replace("'", "''") + "'"
    bind.execute(sa.text(f"ATTACH DATABASE {quoted} AS idx"))


def _table_exists(bind, name, schema="main"):
    insp = sa.inspect(bind)
    return name in insp.get_table_names(schema=schema if schema != "main" else None)


def _count(bind, sql):
    return bind.execute(sa.text(sql)).scalar()


def _log(msg):
    # Plain print: visible both in `flask db upgrade` (update.bat/sh) and
    # in the server log when auto_upgrade runs it at startup.
    print(f"[db-split] {msg}", flush=True)


_COPY_CHUNK = 5000


def _copy_table_chunked(bind, table, src="main", dst="idx", chunk=_COPY_CHUNK):
    """Copy a plain table in id order, logging progress per chunk."""
    total = _count(bind, f"SELECT COUNT(*) FROM {src}.{table}")
    if not total:
        _log(f"{table}: empty, nothing to copy")
        return 0
    copied, last, start = 0, 0, time.monotonic()
    while True:
        n = bind.execute(
            sa.text(f"INSERT INTO {dst}.{table} SELECT * FROM {src}.{table} "
                    f"WHERE id > :last ORDER BY id LIMIT :chunk"),
            {"last": last, "chunk": chunk}).rowcount or 0
        copied += n
        last = bind.execute(
            sa.text(f"SELECT MAX(id) FROM {dst}.{table}")).scalar() or last
        _log(f"{table}: {copied}/{total} "
             f"({copied * 100 // total}%) - "
             f"{time.monotonic() - start:.0f}s elapsed")
        if n < chunk:
            break
    return copied


def _copy_fts_chunked(bind, src="main", dst="idx", chunk=_COPY_CHUNK):
    """Copy the FTS index through the FTS5 API (never the shadow tables),
    in rowid ranges so progress is visible on large libraries."""
    total = _count(bind, f"SELECT COUNT(*) FROM {src}.file_fts")
    if not total:
        _log("file_fts: empty, nothing to copy")
        return 0
    lo = _count(bind, f"SELECT MIN(rowid) FROM {src}.file_fts")
    hi = _count(bind, f"SELECT MAX(rowid) FROM {src}.file_fts")
    copied, start = 0, time.monotonic()
    for first in range(lo, hi + 1, chunk):
        n = bind.execute(
            sa.text(f"INSERT INTO {dst}.file_fts(rowid, name, text, caption, tags) "
                    f"SELECT rowid, name, text, caption, tags "
                    f"FROM {src}.file_fts WHERE rowid BETWEEN :a AND :b"),
            {"a": first, "b": first + chunk - 1}).rowcount or 0
        copied += n
        _log(f"file_fts: {copied}/{total} "
             f"({copied * 100 // total}%) - "
             f"{time.monotonic() - start:.0f}s elapsed")
    return copied


def _file_mb(path):
    try:
        return os.path.getsize(path) / 1048576
    except OSError:
        return 0.0


def upgrade():
    bind = op.get_bind()
    core_path = _core_path()
    if core_path is None:
        return
    if not _table_exists(bind, "file_index"):
        # Fresh database that never had index tables: just make sure the
        # attached file exists with the schema (create_all normally covers
        # this, but migrations-only setups need it here).
        _attach(bind, _index_path(core_path))
        _create_idx_tables()
        return

    index_path = _index_path(core_path)
    _attach(bind, index_path)
    _create_idx_tables()

    n_files = _count(bind, "SELECT COUNT(*) FROM main.files")
    _log(f"starting split: {n_files} files, "
         f"core is {_file_mb(core_path):.0f} MB - "
         f"copying the search index to {os.path.basename(index_path)}")
    t0 = time.monotonic()

    # Idempotent re-runs: clear the destination first.
    op.execute("DELETE FROM idx.file_fts")
    op.execute("DELETE FROM idx.file_embeddings")
    op.execute("DELETE FROM idx.file_index")

    _copy_table_chunked(bind, "file_index")
    _copy_table_chunked(bind, "file_embeddings")
    _copy_fts_chunked(bind)

    for table in ("file_index", "file_embeddings", "file_fts"):
        src = _count(bind, f"SELECT COUNT(*) FROM main.{table}")
        dst = _count(bind, f"SELECT COUNT(*) FROM idx.{table}")
        if src != dst:
            raise RuntimeError(
                f"index split verification failed for {table}: "
                f"main={src} idx={dst}")
        _log(f"verified {table}: {dst} rows match")
    _log(f"copy+verify took {time.monotonic() - t0:.0f}s")

    op.execute("DROP TABLE main.file_fts")
    op.execute("DROP TABLE main.file_index")
    op.execute("DROP TABLE main.file_embeddings")
    _log("old index tables dropped from the core database")
    _log("compacting the core database (VACUUM - can take minutes, "
         "do not interrupt)...")
    with op.get_context().autocommit_block():
        op.execute("VACUUM")
    _log(f"done in {time.monotonic() - t0:.0f}s total: "
         f"core {_file_mb(core_path):.0f} MB + "
         f"index {_file_mb(index_path):.0f} MB")


def downgrade():
    bind = op.get_bind()
    core_path = _core_path()
    if core_path is None:
        return
    if not _table_exists(bind, "file_index", schema="idx"):
        return
    _attach(bind, _index_path(core_path))
    _create_main_tables()
    op.execute("INSERT INTO main.file_index SELECT * FROM idx.file_index")
    op.execute("INSERT INTO main.file_embeddings SELECT * FROM idx.file_embeddings")
    op.execute(
        "INSERT INTO main.file_fts(rowid, name, text, caption, tags) "
        "SELECT rowid, name, text, caption, tags FROM idx.file_fts")
    op.execute("DROP TABLE idx.file_fts")
    op.execute("DROP TABLE idx.file_index")
    op.execute("DROP TABLE idx.file_embeddings")
    with op.get_context().autocommit_block():
        op.execute("VACUUM")


def _create_idx_tables():
    op.create_table(
        'file_index',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.Column('extracted_text', sa.Text(), nullable=True),
        sa.Column('caption', sa.Text(), nullable=True),
        sa.Column('hashtags', sa.Text(), nullable=True),
        sa.Column('word_count', sa.Integer(), nullable=True),
        sa.Column('line_count', sa.Integer(), nullable=True),
        sa.Column('char_count', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('indexed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['file_id'], ['files.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('file_id'),
        schema='idx',
    )
    op.create_index('ix_file_index_file_id', 'file_index', ['file_id'],
                    unique=True, schema='idx')
    op.create_table(
        'file_embeddings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('text_hash', sa.String(length=64), nullable=True),
        sa.Column('embedding', sa.LargeBinary(), nullable=True),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('dims', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['file_id'], ['files.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('file_id', 'chunk_index'),
        schema='idx',
    )
    op.create_index('ix_file_embeddings_file_id', 'file_embeddings',
                    ['file_id'], unique=False, schema='idx')
    op.execute("CREATE VIRTUAL TABLE IF NOT EXISTS idx.file_fts "
               "USING fts5(name, text, caption, tags)")


def _create_main_tables():
    op.create_table(
        'file_index',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.Column('extracted_text', sa.Text(), nullable=True),
        sa.Column('caption', sa.Text(), nullable=True),
        sa.Column('hashtags', sa.Text(), nullable=True),
        sa.Column('word_count', sa.Integer(), nullable=True),
        sa.Column('line_count', sa.Integer(), nullable=True),
        sa.Column('char_count', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('indexed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['file_id'], ['files.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('file_id'),
    )
    op.create_index('ix_file_index_file_id', 'file_index', ['file_id'],
                    unique=True)
    op.create_table(
        'file_embeddings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.Integer(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('text_hash', sa.String(length=64), nullable=True),
        sa.Column('embedding', sa.LargeBinary(), nullable=True),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('dims', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['file_id'], ['files.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('file_id', 'chunk_index'),
    )
    op.create_index('ix_file_embeddings_file_id', 'file_embeddings',
                    ['file_id'], unique=False)
    op.execute("CREATE VIRTUAL TABLE IF NOT EXISTS file_fts "
               "USING fts5(name, text, caption, tags)")
