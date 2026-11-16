"""App init: SQLite connect-time overhead (argon P1 slice B3).

The per-checkout path (NullPool opens a fresh connection per checkout,
e.g. every log_service write via ``db.engine.begin()``) must not redo
persistent work: WAL is set once at startup and lives in the database
files, and a fresh connection never carries the ``idx`` handle, so
DETACH always fails and is pure overhead. ATTACH stays per checkout --
it is per-connection by nature and every fresh connection needs
``idx.*`` (see test_db_split.py).
"""

import sqlite3

from app import _index_db_path, _sqlite_listener


def _trace_checkouts(main_path, idx_path, checkouts=2):
    """checkout_counter + run_two_queries: run `checkouts` fresh-connection
    checkouts through the real `_on_connect` listener, one query each,
    returning every traced statement."""
    listener = _sqlite_listener(idx_path)
    traced = []
    for _ in range(checkouts):
        con = sqlite3.connect(main_path)
        con.set_trace_callback(traced.append)
        try:
            listener(con, None)
            con.execute("SELECT 1").fetchone()
        finally:
            con.set_trace_callback(None)
            con.close()
    return traced


def _journal_mode(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        con.close()


def test_no_redundant_attach_per_checkout(tmp_path):
    main = str(tmp_path / "docindex.sqlite")
    idx = _index_db_path("sqlite:///" + main)
    traced = _trace_checkouts(main, idx, checkouts=2)
    upper = [s.upper() for s in traced]
    detach = sum(s.startswith("DETACH") for s in upper)
    journal = sum("JOURNAL_MODE" in s for s in upper)
    attach = sum(s.startswith("ATTACH") for s in upper)
    assert detach == 0
    assert journal == 0
    # ATTACH itself is still required once per fresh connection.
    assert attach == 2


def test_wal_set_once_at_startup_and_persistent(tmp_path):
    from app import _ensure_persistent_wal

    main = str(tmp_path / "docindex.sqlite")
    idx = _index_db_path("sqlite:///" + main)
    # Startup helper works on pristine paths (files need not exist yet)
    # and is idempotent; :memory: is a safe no-op.
    _ensure_persistent_wal("sqlite:///" + main, idx)
    _ensure_persistent_wal("sqlite:///" + main, idx)
    _ensure_persistent_wal("sqlite:///:memory:", ":memory:")
    # WAL was set once at startup; fresh connections inherit it with no
    # per-checkout PRAGMA.
    assert _journal_mode(main) == "wal"
    assert _journal_mode(idx) == "wal"
