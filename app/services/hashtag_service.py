"""Hashtags: user- and AI-generated searchable tags per file.

Tags live in `FileIndex.hashtags` as a JSON array of plain strings (no '#'
prefix). They are NEVER generated automatically during indexing — only on
explicit user request:

- manually, by the user, in the file view (no word limit);
- per file, from the AI chat (the `set_hashtags` agent tool);
- per drive, with a bulk background job (start/pause/resume/stop), which is
  what `start_job`/`run_job` implement here.

AI-generated tags are limited to `AI_HASHTAG_MAX_WORDS` words each (default
6). Bulk jobs share the AI connection's per-minute rate limit with the chat
(`ai_service._rate_slot` with block=True) and retry provider HTTP 429s.
"""

import json
import re
import threading
import time

from flask import current_app

from ..extensions import db
from ..models import Drive, FileIndex, StoredFile, User
from . import ai_service

MAX_TAGS = 20          # sanity cap on tags per file
MAX_SOURCE_CHARS = 6000  # text excerpt sent to the AI


# --------------------------------------------------------------------------
# Tag storage
# --------------------------------------------------------------------------

def _normalize(tag):
    return re.sub(r"\s+", " ", (tag or "").strip().lstrip("#")).lower()


def get_tags(index):
    """Tags of a FileIndex row as a list of strings."""
    if not index or not index.hashtags:
        return []
    try:
        tags = json.loads(index.hashtags)
    except (ValueError, TypeError):
        return []
    return [t for t in tags if isinstance(t, str)]


def _clean_tags(tags, source):
    """Normalize, dedupe and cap a tag list (AI word limit when source="ai")."""
    max_words = current_app.config.get("AI_HASHTAG_MAX_WORDS", 6)
    clean, seen = [], set()
    for raw in tags or []:
        tag = _normalize(raw)
        if not tag or tag in seen:
            continue
        if source == "ai" and len(tag.split()) > max_words:
            continue
        seen.add(tag)
        clean.append(tag)
        if len(clean) >= MAX_TAGS:
            break
    return clean


def set_tags(stored_file, tags, source="user"):
    """Normalize, dedupe and persist the file's tags. Returns the final list.

    source="ai" enforces the AI_HASHTAG_MAX_WORDS per-tag word limit;
    user-provided tags are not word-limited.
    """
    clean = _clean_tags(tags, source)
    index = stored_file.index
    if index is None:
        index = FileIndex(file_id=stored_file.id, status="pending")
        db.session.add(index)
    index.hashtags = json.dumps(clean)
    db.session.commit()
    return clean


def get_all_tags(user, drive=None):
    """Every hashtag in the user's files with usage counts, most used first.

    Used by the AI agent to discover the existing tag vocabulary before
    searching. Scoped to the current drive when one is given.
    """
    q = (FileIndex.query
         .join(StoredFile, StoredFile.id == FileIndex.file_id)
         .filter(StoredFile.user_id == user.id,
                 StoredFile.deleted_at.is_(None),
                 FileIndex.hashtags.isnot(None)))
    if drive is not None:
        q = q.filter(StoredFile.drive_id == drive.id)
    counts = {}
    for index in q.all():
        for tag in get_tags(index):
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# --------------------------------------------------------------------------
# AI generation
# --------------------------------------------------------------------------

def suggest_tags(stored_file, cfg):
    """Ask the AI for hashtags for one file WITHOUT persisting them.

    Returns the proposed, normalized tag list (AI word limit applied).
    Raises ai_service.AIError on failure.
    """
    index = stored_file.index
    text = ""
    if index:
        text = "\n".join(p for p in (index.caption, index.extracted_text) if p)
    text = text.strip()[:MAX_SOURCE_CHARS]
    max_words = current_app.config.get("AI_HASHTAG_MAX_WORDS", 6)

    messages = [
        {"role": "system", "content": (
            "You generate search hashtags for files. Answer with a "
            "comma-separated list of up to 10 hashtags and nothing else. "
            f"Each hashtag has at most {max_words} words, is lowercase and "
            "has no '#' prefix.")},
        {"role": "user", "content": (
            f"File name: {stored_file.name}\n\n"
            f"Content:\n{text or '(no text extracted — tag from the name)'}")},
    ]
    message = ai_service.chat_completion(messages, config=cfg, block=True)
    raw = (message.get("content") or "").strip()
    return _clean_tags(re.split(r"[,\n;]+", raw), source="ai")


def generate_tags(stored_file, cfg):
    """Ask the AI for hashtags for one file and persist them.

    Returns the final stored tag list. Raises ai_service.AIError on failure.
    """
    return set_tags(stored_file, suggest_tags(stored_file, cfg), source="ai")


def _tag_one(file_id, app):
    """Tag a single file inside its own app context (worker thread entry)."""
    with app.app_context():
        stored = db.session.get(StoredFile, file_id)
        if not stored or stored.deleted_at is not None:
            return False
        cfg = ai_service.config_for(stored.owner)
        attempts = 0
        while True:
            try:
                generate_tags(stored, cfg)
                return True
            except ai_service.AIError as exc:
                attempts += 1
                # Provider HTTP 429: back off and retry; anything else fails.
                if "429" in str(exc) and attempts < 3:
                    time.sleep(20)
                    continue
                raise


# --------------------------------------------------------------------------
# Bulk per-drive background job
# --------------------------------------------------------------------------

class _HashtagCancelled(Exception):
    """Raised inside a bulk tagging job when the user cancels it."""


class _HashtagJob:
    """Progress/state of one bulk tagging run (in-memory only)."""

    def __init__(self, drive_id, drive_name):
        self.drive_id = drive_id
        self.drive_name = drive_name
        self.state = "running"      # running | paused | done | cancelled | error
        self.total = 0              # files to tag
        self.processed = 0
        self.current = ""           # file being tagged right now
        self.stats = {"tagged": 0, "skipped": 0, "failed": 0}
        self.error = None
        self.thread = None
        self._resume = threading.Event()
        self._resume.set()          # cleared while paused
        self._cancelled = False

    @property
    def percent(self):
        if not self.total:
            return 0
        return round(self.processed * 100 / self.total)

    def cancel(self):
        self._cancelled = True
        self._resume.set()          # wake paused workers so they can exit

    def checkpoint(self):
        """Block while paused; abort the job when cancelled."""
        self._resume.wait()
        if self._cancelled:
            raise _HashtagCancelled()

    def as_dict(self):
        return {
            "drive_id": self.drive_id,
            "drive_name": self.drive_name,
            "state": self.state,
            "total": self.total,
            "processed": self.processed,
            "percent": self.percent,
            "current": self.current,
            "stats": self.stats,
            "error": self.error,
        }


_jobs = {}  # drive_id -> _HashtagJob
_jobs_lock = threading.Lock()


def get_status(drive_id):
    with _jobs_lock:
        job = _jobs.get(drive_id)
        return job.as_dict() if job else None


def get_active_job(user_id):
    """The user's currently running/paused tagging job, if any (widget)."""
    with _jobs_lock:
        candidates = [j for j in _jobs.values() if j.state in ("running", "paused")]
    if not candidates:
        return None
    drive_ids = [j.drive_id for j in candidates]
    owned = {d.id for d in Drive.query.filter(Drive.id.in_(drive_ids),
                                              Drive.user_id == user_id)}
    for job in candidates:
        if job.drive_id in owned:
            return job.as_dict()
    return None


def pause_job(drive_id):
    with _jobs_lock:
        job = _jobs.get(drive_id)
        if job and job.state == "running":
            job.state = "paused"
            job._resume.clear()
            return True
    return False


def resume_job(drive_id):
    with _jobs_lock:
        job = _jobs.get(drive_id)
        if job and job.state == "paused":
            job.state = "running"
            job._resume.set()
            return True
    return False


def cancel_job(drive_id):
    """Stop a running/paused job. Workers exit at the next file."""
    with _jobs_lock:
        job = _jobs.get(drive_id)
        if job and job.state in ("running", "paused"):
            job.cancel()
            return True
    return False


def start_job(drive, workers=1, overwrite=False, app=None):
    """Start a bulk tagging job for the drive. Returns the job.

    Runs in background threads unless HASHTAG_ASYNC is off (tests), in which
    case it runs inline and the returned job is already finished.
    """
    with _jobs_lock:
        existing = _jobs.get(drive.id)
        if existing and existing.state in ("running", "paused"):
            return existing
        job = _HashtagJob(drive.id, drive.name)
        _jobs[drive.id] = job

    app = app or current_app._get_current_object()
    workers = max(1, min(int(workers or 1), 8))
    if app.config.get("HASHTAG_ASYNC", True):
        thread = threading.Thread(target=_job_worker,
                                  args=(drive.id, workers, bool(overwrite), job, app),
                                  daemon=True)
        job.thread = thread
        thread.start()
    else:
        _job_worker(drive.id, workers, bool(overwrite), job, app)
    return job


def _job_worker(drive_id, workers, overwrite, job, app):
    with app.app_context():
        try:
            drive = db.session.get(Drive, drive_id)
            if drive is None:
                job.state = "cancelled"
                return
            run_job(drive, job=job, workers=workers, overwrite=overwrite, app=app)
            job.state = "done"
        except _HashtagCancelled:
            db.session.rollback()
            job.state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - report to the widget
            db.session.rollback()
            app.logger.exception("Hashtag job failed for drive %s", drive_id)
            job.state = "error"
            job.error = str(exc)[:500]


def run_job(drive, job=None, workers=1, overwrite=False, app=None):
    """Tag every eligible file in the drive. Synchronous core.

    Files that already have tags are skipped unless `overwrite` is set.
    Returns stats: {tagged, skipped, failed}.
    """
    app = app or current_app._get_current_object()
    user = db.session.get(User, drive.user_id)
    if not ai_service.is_enabled(user):
        raise ValueError("AI is not configured. Add a connection in AI Settings.")

    files = (StoredFile.query
             .filter_by(drive_id=drive.id)
             .filter(StoredFile.deleted_at.is_(None))
             .order_by(StoredFile.id).all())
    if overwrite:
        todo = files
        skipped = 0
    else:
        todo = [f for f in files if not get_tags(f.index)]
        skipped = len(files) - len(todo)

    stats = job.stats if job else {"tagged": 0, "skipped": 0, "failed": 0}
    stats["skipped"] += skipped
    if job:
        job.total = len(todo)

    queue = list(f.id for f in todo)
    names = {f.id: f.name for f in todo}
    lock = threading.Lock()

    def worker():
        while True:
            if job:
                job.checkpoint()
            with lock:
                if not queue:
                    return
                file_id = queue.pop(0)
                if job:
                    job.current = names.get(file_id, "")
            try:
                _tag_one(file_id, app)
                with lock:
                    stats["tagged"] += 1
            except _HashtagCancelled:
                raise
            except Exception:  # noqa: BLE001 - one bad file must not kill the job
                app.logger.exception("Hashtag generation failed for file %s", file_id)
                with lock:
                    stats["failed"] += 1
            finally:
                if job:
                    with lock:
                        job.processed += 1

    if app.config.get("HASHTAG_ASYNC", True):
        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(min(workers, max(1, len(queue))))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        # Tests (in-memory SQLite): same thread, sequential.
        worker()
    return stats
