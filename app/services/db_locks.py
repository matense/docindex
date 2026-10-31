"""Process-wide serialization for search-index writes.

SQLite has a single writer per database file: concurrent index drainers,
embedding jobs, tag updates and web-request deletes used to hit
"database is locked" (or pile up in long lock waits) against each other.
Reads never block under WAL, so only *writes* to the index database go
through this reentrant lock. It is intentionally coarse: one writer at a
time, held only for the commit-sized tail of each job (extraction itself
stays parallel). The 30 s busy timeout remains as a backstop for anything
outside the lock (e.g. a second process).
"""

import functools
import threading
from contextlib import contextmanager

_index_write_lock = threading.RLock()


@contextmanager
def index_write():
    """Serialize one index-database write transaction."""
    with _index_write_lock:
        yield


def serialized(fn):
    """Decorator: run the whole function under the index write lock.

    Reentrant (RLock), so locked functions can safely call each other
    (e.g. index_file -> fts_upsert -> embed). Apply to every function
    that commits index-database writes; reads stay lock-free (WAL).
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _index_write_lock:
            return fn(*args, **kwargs)
    return wrapper
