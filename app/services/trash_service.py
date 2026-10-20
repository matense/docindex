"""Trash auto-purge: permanently delete items older than 30 days.

Opt-in per user (`User.trash_auto_purge`, default off, toggled on the
trash page). A daemon thread wakes hourly; each run purges the user's
trashed files and whole trashed drives older than the retention window.

Files go through ``file_service.purge_file`` (blobs, versions, FTS and
embedding rows); drives go through ``drive_service.purge_drive``. Synced
drives never enter the trash (their remove path is immediate and never
touches the real folder), so there is nothing to auto-purge for them.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from flask import current_app

from ..extensions import db
from ..models import Drive, StoredFile, User

TRASH_RETENTION_DAYS = 30


def _cutoff():
    return datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS)


def purge_expired(app=None):
    """Purge every opted-in user's trash older than 30 days. Returns
    (files_purged, drives_purged). Safe to call from tests (no threads)."""
    app = app or current_app._get_current_object()
    from . import drive_service, file_service

    files_purged = drives_purged = 0
    with app.app_context():
        users = User.query.filter_by(trash_auto_purge=True).all()
        for user in users:
            cutoff = _cutoff()
            naive_cutoff = cutoff.replace(tzinfo=None)
            old_files = (StoredFile.query
                         .filter(StoredFile.user_id == user.id,
                                 StoredFile.deleted_at.isnot(None),
                                 StoredFile.deleted_at < naive_cutoff).all())
            for stored in old_files:
                try:
                    file_service.purge_file(stored)
                    files_purged += 1
                except Exception:  # noqa: BLE001 - keep purging the rest
                    db.session.rollback()
                    app.logger.exception(
                        "Auto-purge failed for file %s", stored.id)
            old_drives = (Drive.query
                          .filter(Drive.user_id == user.id,
                                  Drive.deleted_at.isnot(None),
                                  Drive.deleted_at < naive_cutoff).all())
            for drive in old_drives:
                try:
                    if drive_service.purge_drive(user, drive.id):
                        drives_purged += 1
                except Exception:  # noqa: BLE001 - keep purging the rest
                    db.session.rollback()
                    app.logger.exception(
                        "Auto-purge failed for drive %s", drive.id)
        return files_purged, drives_purged


_started = False
_lock = threading.Lock()


def _loop(app, interval_seconds=3600):
    while True:
        time.sleep(interval_seconds)
        try:
            files_purged, drives_purged = purge_expired(app)
            if files_purged or drives_purged:
                app.logger.info(
                    "Auto-purged %d file(s) and %d drive(s) older than %d days.",
                    files_purged, drives_purged, TRASH_RETENTION_DAYS)
        except Exception:  # noqa: BLE001 - the janitor must never die
            app.logger.exception("Trash auto-purge tick failed")


def start_auto_purge(app, interval_seconds=3600):
    """Start the janitor thread once per process (idempotent)."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(app, interval_seconds),
                     daemon=True).start()
