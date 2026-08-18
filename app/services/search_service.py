import re

from markupsafe import Markup, escape
from sqlalchemy import or_

from ..models import FileIndex, StoredFile

SNIPPET_RADIUS = 120
MAX_RESULTS = 50


def _terms(query):
    return [t for t in re.split(r"\s+", query.strip()) if t][:8]


def _score(stored_file, index, terms):
    score = 0
    name_l = stored_file.name.lower()
    text_l = ((index.extracted_text if index else None) or "").lower()
    caption_l = ((index.caption if index else None) or "").lower()
    for term in terms:
        t = term.lower()
        if t in name_l:
            score += 10
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


def search_files(query, user, limit=MAX_RESULTS, drive=None):
    """Full-text search over a user's files. Returns list of result dicts.

    When `drive` is given, only files in that drive are searched.
    """
    terms = _terms(query)
    if not terms:
        return []

    conditions = []
    for term in terms:
        like = f"%{term}%"
        conditions.append(StoredFile.name.ilike(like))
        conditions.append(FileIndex.extracted_text.ilike(like))
        conditions.append(FileIndex.caption.ilike(like))

    q = (StoredFile.query
         .outerjoin(FileIndex, FileIndex.file_id == StoredFile.id)
         .filter(StoredFile.user_id == user.id,
                 StoredFile.deleted_at.is_(None)))
    if drive is not None:
        q = q.filter(StoredFile.drive_id == drive.id)
    rows = q.filter(or_(*conditions)).all()

    results = []
    for stored in rows:
        index = stored.index
        score = _score(stored, index, terms)
        if score == 0:
            continue
        text_l = ((index.extracted_text if index else None) or "").lower()
        caption_l = ((index.caption if index else None) or "").lower()
        name_l = stored.name.lower()
        terms_l = [t.lower() for t in terms]
        matches = []
        if any(t in name_l for t in terms_l):
            matches.append("name")
        if caption_l and any(t in caption_l for t in terms_l):
            matches.append("caption")
        if text_l and any(t in text_l for t in terms_l):
            matches.append("content")
        text_source = None
        if index:
            text_source = index.caption or index.extracted_text
        results.append({
            "file": stored,
            "score": score,
            "snippet": _make_snippet(text_source or stored.name, terms),
            "name_html": _highlight(stored.name, terms),
            "matches": matches,
            "caption": index.caption if index else None,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]
