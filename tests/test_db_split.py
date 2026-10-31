"""Split-database layout: index tables live in the attached `idx` db."""

import io
import os
import threading

from sqlalchemy import text as sql_text

from app import _index_db_path
from app.extensions import db
from app.models import FileEmbedding, FileIndex, StoredFile, User
from app.services import search_service
from app.services.db_locks import index_write, serialized


def _upload(client, name, content):
    return client.post("/upload", data={
        "folder_id": "",
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def test_index_models_live_in_idx_schema():
    assert FileIndex.__table__.schema == "idx"
    assert FileEmbedding.__table__.schema == "idx"


def test_index_db_path_derivation():
    assert _index_db_path("sqlite:///:memory:") == ":memory:"
    assert _index_db_path("sqlite:///C:/data/docindex.sqlite") == \
        "C:/data/docindex_index.sqlite"
    assert _index_db_path("postgresql://u@h/db") is None


def test_index_tables_created_in_attached_db(app):
    with app.app_context():
        # The FTS virtual table is created lazily by the availability
        # probe (same as before the split) — trigger it explicitly.
        assert search_service.fts_available() is True
        main_tables = {r[0] for r in db.session.execute(
            sql_text("SELECT name FROM sqlite_master "
                     "WHERE type='table'")).all()}
        idx_tables = {r[0] for r in db.session.execute(
            sql_text("SELECT name FROM idx.sqlite_master "
                     "WHERE type='table'")).all()}
    assert "file_index" not in main_tables
    assert "file_fts" not in main_tables
    assert "file_embeddings" not in main_tables
    assert "file_index" in idx_tables
    assert "file_embeddings" in idx_tables
    assert "file_fts" in idx_tables


def test_upload_index_and_fts_search_use_index_db(auth_client, app, user):
    resp = _upload(auth_client, "split-test.txt",
                   b"xylophone quantum split database test")
    assert resp.status_code == 200
    with app.app_context():
        user_obj = db.session.get(User, user)
        stored = StoredFile.query.filter_by(name="split-test.txt").one()
        # ORM access is unchanged; the row physically lives in idx.
        assert stored.index is not None
        assert stored.index.status == "ok"
        assert "xylophone" in (stored.index.extracted_text or "")
        fts_count = db.session.execute(
            sql_text("SELECT COUNT(*) FROM idx.file_fts")).scalar()
        assert fts_count >= 1
        # FTS search (cross-db join) finds the file.
        results = search_service.search_files("xylophone", user_obj)
        assert any(r["file"].id == stored.id for r in results)
        # ...and the advanced path with MATCH/rank works too.
        adv = search_service.search_advanced(user_obj, "xylophone", limit=5)
        assert adv["total"] >= 1


def test_index_write_lock_serializes_threads():
    counter = {"n": 0}

    @serialized
    def bump(times):
        for _ in range(times):
            counter["n"] += 1

    threads = [threading.Thread(target=bump, args=(200,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter["n"] == 1000


def test_index_write_context_manager_is_reentrant():
    with index_write():
        with index_write():
            pass


def _load_split_migration():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "migrations",
                        "versions", "f1e2d3c4b5a6_split_index_db.py")
    spec = importlib.util.spec_from_file_location("split_index_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_chunked_copy_helpers_move_tables_and_fts():
    import sqlalchemy as sa

    mig = _load_split_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("ATTACH DATABASE ':memory:' AS idx"))
        conn.execute(sa.text(
            "CREATE TABLE file_index (id INTEGER PRIMARY KEY, "
            "file_id INTEGER, extracted_text TEXT)"))
        conn.execute(sa.text(
            "CREATE TABLE file_embeddings (id INTEGER PRIMARY KEY, "
            "file_id INTEGER, chunk_index INTEGER)"))
        conn.execute(sa.text(
            "CREATE VIRTUAL TABLE file_fts USING fts5(name, text, caption, tags)"))
        conn.execute(sa.text(
            "CREATE TABLE idx.file_index (id INTEGER PRIMARY KEY, "
            "file_id INTEGER, extracted_text TEXT)"))
        conn.execute(sa.text(
            "CREATE TABLE idx.file_embeddings (id INTEGER PRIMARY KEY, "
            "file_id INTEGER, chunk_index INTEGER)"))
        conn.execute(sa.text(
            "CREATE VIRTUAL TABLE idx.file_fts "
            "USING fts5(name, text, caption, tags)"))
        for i in range(1, 12):
            conn.execute(
                sa.text("INSERT INTO file_index VALUES (:i, :i, :t)"),
                {"i": i, "t": f"text number {i}"})
            conn.execute(sa.text(
                "INSERT INTO file_fts(rowid, name, text, caption, tags) "
                "VALUES (:i, :n, :t, '', '')"),
                {"i": i, "n": f"f{i}.txt", "t": f"text number {i}"})
        assert mig._copy_table_chunked(conn, "file_index", chunk=5) == 11
        assert mig._copy_table_chunked(conn, "file_embeddings") == 0
        assert mig._copy_fts_chunked(conn, chunk=5) == 11
        assert conn.execute(
            sa.text("SELECT COUNT(*) FROM idx.file_index")).scalar() == 11
        assert conn.execute(sa.text(
            "SELECT text FROM idx.file_fts WHERE file_fts MATCH 'number'"
        )).fetchall()
        # Idempotent: clear + re-copy gives the same counts.
        conn.execute(sa.text("DELETE FROM idx.file_fts"))
        conn.execute(sa.text("DELETE FROM idx.file_index"))
        assert mig._copy_table_chunked(conn, "file_index") == 11
        assert mig._copy_fts_chunked(conn) == 11
