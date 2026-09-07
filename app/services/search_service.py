import re

from flask import current_app
from markupsafe import Markup, escape
from sqlalchemy import or_
from sqlalchemy import text as sql_text

from ..models import FileIndex, StoredFile
from ..extensions import db
from . import hashtag_service

SNIPPET_RADIUS = 120
MAX_RESULTS = 50


# --------------------------------------------------------------------------
# FTS5 index (file_fts virtual table; rowid = files.id)
# --------------------------------------------------------------------------

_fts_ready = None  # WeakKeyDictionary engine -> bool, initialized lazily


def fts_available():
    """True when SQLite FTS5 is usable and SEARCH_FTS is on.

    Probed once per engine (each app/test gets its own): the probe also
    creates the file_fts table when missing (IF NOT EXISTS), so databases
    built with db.create_all() (no migrations) get it too.
    """
    global _fts_ready
    if not current_app.config.get("SEARCH_FTS", True):
        return False
    if _fts_ready is None:
        import weakref
        _fts_ready = weakref.WeakKeyDictionary()
    engine = db.engine
    ready = _fts_ready.get(engine)
    if ready is None:
        try:
            with engine.begin() as conn:
                conn.execute(sql_text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS file_fts "
                    "USING fts5(name, text, caption, tags)"))
            ready = True
        except Exception:  # noqa: BLE001 - any failure means: no FTS5
            ready = False
        _fts_ready[engine] = ready
    return ready


def fts_upsert(file_id):
    """Refresh a file's FTS row from files/file_index. No-op without FTS5."""
    if not fts_available():
        return
    stored = db.session.get(StoredFile, file_id)
    if not stored or stored.deleted_at is not None:
        fts_delete(file_id)
        return
    index = stored.index
    db.session.execute(sql_text(
        "INSERT OR REPLACE INTO file_fts(rowid, name, text, caption, tags) "
        "VALUES (:id, :name, :text, :caption, :tags)"),
        {"id": stored.id, "name": stored.name,
         "text": (index.extracted_text if index else "") or "",
         "caption": (index.caption if index else "") or "",
         "tags": (index.hashtags if index else "") or ""})
    db.session.commit()


def fts_delete(file_id):
    if not fts_available():
        return
    db.session.execute(sql_text("DELETE FROM file_fts WHERE rowid = :id"),
                       {"id": file_id})
    db.session.commit()


_FTS_BATCH = 500  # keep well under SQLite's bound-variable limit


def fts_delete_many(file_ids):
    """Delete many FTS rows in one go (batched — SQLite caps the number of
    bound variables per statement). No-op without FTS5."""
    if not fts_available():
        return
    ids = list(file_ids)
    for i in range(0, len(ids), _FTS_BATCH):
        chunk = ids[i:i + _FTS_BATCH]
        db.session.execute(sql_text(
            "DELETE FROM file_fts WHERE rowid IN "
            "(" + ", ".join(str(int(fid)) for fid in chunk) + ")"))
    db.session.commit()


def fts_rebuild():
    """Drop and repopulate the whole FTS index from file_index (the source
    of truth). Returns the number of indexed rows."""
    if not fts_available():
        return 0
    db.session.execute(sql_text("DELETE FROM file_fts"))
    db.session.execute(sql_text(
        "INSERT INTO file_fts(rowid, name, text, caption, tags) "
        "SELECT f.id, f.name, "
        "       COALESCE(fi.extracted_text, ''), "
        "       COALESCE(fi.caption, ''), "
        "       COALESCE(fi.hashtags, '') "
        "FROM files f LEFT JOIN file_index fi ON fi.file_id = f.id "
        "WHERE f.deleted_at IS NULL"))
    db.session.commit()
    return db.session.execute(sql_text("SELECT count(*) FROM file_fts")).scalar()


def _fts_query(terms):
    """Build a safe MATCH expression: each term becomes a quoted prefix."""
    return " AND ".join('"%s"*' % t.replace('"', '""') for t in terms)


def _fts_candidates(terms, user, drive, limit):
    """file ids matching all terms via FTS5, best BM25 rank first.

    Returns None when the MATCH query fails (odd operator characters) so the
    caller falls back to ILIKE.
    """
    sql = ("SELECT file_fts.rowid AS file_id, "
           "       bm25(file_fts, 0.2, 1.0, 0.6, 0.4) AS rank "
           "FROM file_fts JOIN files ON files.id = file_fts.rowid "
           "WHERE file_fts MATCH :match "
           "  AND files.user_id = :uid AND files.deleted_at IS NULL")
    params = {"match": _fts_query(terms), "uid": user.id}
    if drive is not None:
        sql += " AND files.drive_id = :did"
        params["did"] = drive.id
    sql += " ORDER BY rank LIMIT :lim"
    params["lim"] = max(limit * 4, 50)
    try:
        return db.session.execute(sql_text(sql), params).all()
    except Exception:  # noqa: BLE001 - bad MATCH syntax etc.
        db.session.rollback()
        return None


def _terms(query):
    return [t for t in re.split(r"\s+", query.strip()) if t][:8]


def _score(stored_file, index, terms):
    score = 0
    name_l = stored_file.name.lower()
    text_l = ((index.extracted_text if index else None) or "").lower()
    caption_l = ((index.caption if index else None) or "").lower()
    tags_l = ((index.hashtags if index else None) or "").lower()
    for term in terms:
        t = term.lower()
        if t in name_l:
            score += 10
        if t in tags_l:
            score += 7
        if t in caption_l:
            score += 5
        if t in text_l:
            score += 2 + min(text_l.count(t), 10)
    return score


def _highlight(text, terms):
    """Escape `text` and wrap every term occurrence in <mark>. Returns Markup."""
    highlighted = escape(text)
    for term in terms:
        highlighted = Markup(re.sub(
            re.escape(escape(term)),
            lambda m: f"<mark>{m.group(0)}</mark>",
            highlighted,
            flags=re.IGNORECASE,
        ))
    return highlighted


def _make_snippet(text, terms):
    if not text:
        return Markup("")
    text_l = text.lower()
    pos = -1
    for term in terms:
        pos = text_l.find(term.lower())
        if pos != -1:
            break
    if pos == -1:
        return escape(text[: 2 * SNIPPET_RADIUS])
    start = max(0, pos - SNIPPET_RADIUS)
    end = min(len(text), pos + SNIPPET_RADIUS)
    prefix = "&hellip; " if start > 0 else ""
    suffix = " &hellip;" if end < len(text) else ""
    return Markup(prefix + str(_highlight(text[start:end], terms)) + suffix)


def _build_result(stored, terms, extra_score=0.0):
    """Assemble one search result dict (snippet, badges, tags) for a file
    already known to match. `extra_score` is a small tie-breaker (FTS rank)."""
    index = stored.index
    score = _score(stored, index, terms) + extra_score
    text_l = ((index.extracted_text if index else None) or "").lower()
    caption_l = ((index.caption if index else None) or "").lower()
    tags_l = ((index.hashtags if index else None) or "").lower()
    name_l = stored.name.lower()
    terms_l = [t.lower() for t in terms]
    matches = []
    if any(t in name_l for t in terms_l):
        matches.append("name")
    if tags_l and any(t in tags_l for t in terms_l):
        matches.append("tags")
    if caption_l and any(t in caption_l for t in terms_l):
        matches.append("caption")
    if text_l and any(t in text_l for t in terms_l):
        matches.append("content")
    text_source = None
    if index:
        text_source = index.caption or index.extracted_text
    return {
        "file": stored,
        "score": score,
        "snippet": _make_snippet(text_source or stored.name, terms),
        "name_html": _highlight(stored.name, terms),
        "matches": matches,
        "caption": index.caption if index else None,
        "tags": hashtag_service.get_tags(index),
    }


def search_files(query, user, limit=MAX_RESULTS, drive=None):
    """Full-text search over a user's files. Returns list of result dicts.

    Uses the FTS5 index (BM25 ranking, prefix matching) when available and
    falls back to ILIKE scanning otherwise. When `drive` is given, only
    files in that drive are searched.
    """
    terms = _terms(query)
    if not terms:
        return []

    if fts_available():
        rows = _fts_candidates(terms, user, drive, limit)
        if rows is not None:
            rank_by_id = {r.file_id: r.rank for r in rows}
            ids = list(rank_by_id)
            files = (StoredFile.query.filter(StoredFile.id.in_(ids)).all()
                     if ids else [])
            results = [
                _build_result(f, terms,
                              extra_score=min(-rank_by_id[f.id], 10.0) / 10.0)
                for f in files
            ]
            # Python boosts keep the name>tags>caption>content dominance;
            # the BM25 fraction only breaks ties.
            results.sort(key=lambda r: r["score"], reverse=True)
            return results[:limit]

    # --- ILIKE fallback (also used when FTS5 is unavailable) ---
    conditions = []
    for term in terms:
        like = f"%{term}%"
        conditions.append(StoredFile.name.ilike(like))
        conditions.append(FileIndex.extracted_text.ilike(like))
        conditions.append(FileIndex.caption.ilike(like))
        conditions.append(FileIndex.hashtags.ilike(like))

    q = (StoredFile.query
         .outerjoin(FileIndex, FileIndex.file_id == StoredFile.id)
         .filter(StoredFile.user_id == user.id,
                 StoredFile.deleted_at.is_(None)))
    if drive is not None:
        q = q.filter(StoredFile.drive_id == drive.id)
    rows = q.filter(or_(*conditions)).all()

    results = []
    for stored in rows:
        if _score(stored, stored.index, terms) == 0:
            continue
        results.append(_build_result(stored, terms))

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


# --------------------------------------------------------------------------
# Advanced search (agent-oriented): FTS5 syntax, metadata filters, sort and
# pagination. Additive — the UI search above is unchanged.
# --------------------------------------------------------------------------

MAX_ADVANCED_LIMIT = 25

# Quotes, prefixes, column scopes and boolean operators mark a query as raw
# FTS5 syntax; anything else takes the safe term-prefix path used by the UI.
_FTS5_SYNTAX_RE = re.compile(r'"|\*|:|\bAND\b|\bOR\b|\bNOT\b', re.IGNORECASE)

_ADVANCED_SORTS = {
    "relevance": "rank",
    "name": "files.name COLLATE NOCASE",
    "size": "files.size DESC",
    "word_count": "fi.word_count DESC",
    "updated_at": "files.updated_at DESC",
}

_BOOL_WORDS = {"and", "or", "not"}


def _folder_subtree_ids(folder_id, user_id):
    """A folder id plus all its descendant folder ids (iterative, batched)."""
    from ..models import Folder
    ids, frontier = [], [folder_id]
    while frontier:
        ids.extend(frontier)
        frontier = [f.id for f in Folder.query.filter(
            Folder.user_id == user_id,
            Folder.parent_id.in_(frontier)).all()]
    return ids


def _advanced_clauses(user, drive=None, extensions=None, folder_id=None,
                      recursive=False, min_words=None, max_words=None,
                      min_size=None, max_size=None, has_tags=False,
                      has_caption=False, indexed=False):
    """SQL WHERE fragments + params shared by search_advanced/count_files."""
    clauses = ["files.user_id = :uid", "files.deleted_at IS NULL"]
    params = {"uid": user.id}
    if drive is not None:
        clauses.append("files.drive_id = :did")
        params["did"] = drive.id
    if extensions:
        exts = [str(e).lower().lstrip(".") for e in extensions][:20]
        if exts:
            clauses.append("files.extension IN ("
                           + ", ".join(f":ext{i}" for i in range(len(exts)))
                           + ")")
            params.update({f"ext{i}": e for i, e in enumerate(exts)})
    if folder_id is not None:
        ids = (_folder_subtree_ids(folder_id, user.id) if recursive
               else [folder_id])
        clauses.append("files.folder_id IN ("
                       + ", ".join(f":fol{i}" for i in range(len(ids))) + ")")
        params.update({f"fol{i}": fid for i, fid in enumerate(ids)})
    if min_words is not None:
        clauses.append("fi.word_count >= :min_words")
        params["min_words"] = int(min_words)
    if max_words is not None:
        clauses.append("fi.word_count <= :max_words")
        params["max_words"] = int(max_words)
    if min_size is not None:
        clauses.append("files.size >= :min_size")
        params["min_size"] = int(min_size)
    if max_size is not None:
        clauses.append("files.size <= :max_size")
        params["max_size"] = int(max_size)
    if has_tags:
        clauses.append("fi.hashtags IS NOT NULL AND fi.hashtags != ''")
    if has_caption:
        clauses.append("fi.caption IS NOT NULL AND fi.caption != ''")
    if indexed:
        clauses.append("fi.status = 'ok'")
    return " AND ".join(f"({c})" for c in clauses), params


def _plain_terms(query):
    """Plain-word terms extracted from any query (drops FTS5 operators) —
    used for snippets and for the ILIKE fallback."""
    return [t for t in re.findall(r"[\w]+", (query or "").lower())
            if t not in _BOOL_WORDS][:8]


def _advanced_base(user, query, filters):
    """(FROM..WHERE sql, params, terms, used_fts) for the advanced search.

    FTS5 syntax queries go straight to MATCH; plain queries become the safe
    term-prefix AND. Falls back to ILIKE when FTS5 is off or the MATCH is
    rejected by SQLite (bad syntax — the caller keeps working with plain
    terms instead of failing).
    """
    where, params = _advanced_clauses(user, **filters)
    terms = _plain_terms(query)
    if query and fts_available():
        base = ("FROM file_fts JOIN files ON files.id = file_fts.rowid "
                "LEFT JOIN file_index fi ON fi.file_id = files.id "
                f"WHERE file_fts MATCH :match AND {where}")
        # Raw FTS5 syntax first; if SQLite rejects it, retry with the safe
        # term-prefix query built from the plain words in the query — the
        # caller still gets useful results instead of an error.
        candidates = []
        if _FTS5_SYNTAX_RE.search(query):
            candidates.append(query)
        if terms:
            candidates.append(_fts_query(terms))
        for match in candidates:
            probe = dict(params, match=match)
            try:
                db.session.execute(
                    sql_text(f"SELECT 1 {base} LIMIT 1"), probe).first()
                return base, probe, terms, True
            except Exception:  # noqa: BLE001 - invalid MATCH syntax
                db.session.rollback()
    base = ("FROM files LEFT JOIN file_index fi ON fi.file_id = files.id "
            f"WHERE {where}")
    if query and terms:
        likes = []
        for i, t in enumerate(terms):
            key = f"term{i}"
            params[key] = f"%{t}%"
            likes.append(f"(LOWER(files.name) LIKE LOWER(:{key}) "
                         f"OR LOWER(fi.extracted_text) LIKE LOWER(:{key}) "
                         f"OR LOWER(fi.caption) LIKE LOWER(:{key}) "
                         f"OR LOWER(fi.hashtags) LIKE LOWER(:{key}))")
        base += " AND " + " AND ".join(likes)
    return base, params, terms, False


def _plain_snippet(text, terms, radius=160):
    """One-line agent snippet around the first term hit: [bracket] markers,
    no HTML, whitespace collapsed."""
    if not text:
        return ""
    flat = " ".join(text.split())
    flat_l = flat.lower()
    pos = -1
    for t in terms or []:
        pos = flat_l.find(t.lower())
        if pos != -1:
            break
    if pos == -1:
        return flat[: 2 * radius]
    start = max(0, pos - radius // 2)
    end = min(len(flat), pos + radius)
    snip = flat[start:end]
    for t in terms or []:
        snip = re.sub(re.escape(t), lambda m: f"[{m.group(0)}]", snip,
                      flags=re.IGNORECASE)
    return ("… " if start else "") + snip + (" …" if end < len(flat) else "")


def search_advanced(user, query=None, *, sort="relevance", offset=0, limit=10,
                    **filters):
    """Agent-oriented search: FTS5 syntax (phrases, AND/OR/NOT, column scopes
    like name:/tags:/caption:), metadata filters (drive, extensions, folder,
    word/size ranges, has_tags, has_caption, indexed), sort and pagination.

    Returns {"results": [compact dicts], "total": <rows matching the query
    and filters, ignoring pagination>}.
    """
    limit = max(1, min(int(limit or 10), MAX_ADVANCED_LIMIT))
    offset = max(0, int(offset or 0))
    query = (query or "").strip()
    base, params, terms, used_fts = _advanced_base(user, query, filters)
    order = _ADVANCED_SORTS.get(sort, "rank")
    if order == "rank" and not used_fts:
        order = "files.name COLLATE NOCASE"  # no BM25 without FTS

    total = db.session.execute(
        sql_text(f"SELECT COUNT(*) {base}"), params).scalar()
    select = ("SELECT files.id AS fid, "
              + ("bm25(file_fts, 0.2, 1.0, 0.6, 0.4) AS rank, "
                 "snippet(file_fts, 1, '[', ']', '…', 16) AS snip "
                 if used_fts else "NULL AS rank, NULL AS snip ")
              + base)
    rows = db.session.execute(
        sql_text(f"{select} ORDER BY {order} LIMIT :lim OFFSET :off"),
        dict(params, lim=limit, off=offset)).all()

    files = {f.id: f for f in StoredFile.query.filter(
        StoredFile.id.in_([r.fid for r in rows])).all()} if rows else {}
    results = []
    for r in rows:
        stored = files.get(r.fid)
        if stored is None:
            continue
        index = stored.index
        snip = r.snip
        if snip is None:
            snip = _plain_snippet(
                ((index.extracted_text if index else "")
                 or (index.caption if index else "") or ""), terms)
        results.append({
            "file_id": stored.id,
            "name": stored.name,
            "extension": stored.extension,
            "size": stored.size,
            "word_count": index.word_count if index else None,
            "score": round(min(-(r.rank or 0.0), 10.0), 3),
            "snippet": snip,
            "tags": hashtag_service.get_tags(index),
        })
    return {"results": results, "total": total}


def count_files(user, query=None, **filters):
    """Aggregate view of a search WITHOUT reading any content:
    {"total": n, "by_extension": {...}, "by_drive": {...}}."""
    query = (query or "").strip()
    base, params, _terms_, _used_fts = _advanced_base(user, query, filters)
    total = db.session.execute(
        sql_text(f"SELECT COUNT(*) {base}"), params).scalar()
    # Breakdowns aggregate over the same match set (subquery in FROM — JOINs
    # cannot be appended after the WHERE that `base` already carries).
    sub = f"(SELECT files.drive_id AS did, files.extension AS ext {base})"
    by_extension = {ext or "": n for ext, n in db.session.execute(
        sql_text(f"SELECT ext, COUNT(*) FROM {sub} GROUP BY ext"),
        params).all()}
    by_drive = {name: n for name, n in db.session.execute(
        sql_text(f"SELECT d.name, COUNT(*) FROM {sub} s "
                 "JOIN drives d ON d.id = s.did GROUP BY d.name"),
        params).all()}
    return {"total": total, "by_extension": by_extension,
            "by_drive": by_drive}
