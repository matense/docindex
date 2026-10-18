from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required
from markupsafe import escape

from ..models import Drive, StoredFile
from ..services import drive_service, embedding_service, hashtag_service, search_service

bp = Blueprint("search", __name__)


@bp.route("/search")
@login_required
def results():
    if request.args.get("mode") == "ai":
        return render_template("search/ai.html")
    query = request.args.get("q", "").strip()
    if query:
        drive = drive_service.get_current_drive(current_user)
        embeddings_on = embedding_service.embeddings_available(current_user)
        mode = (request.args.get("mode") or "auto").strip().lower()
        if mode not in ("auto", "keyword", "hybrid", "semantic"):
            mode = "auto"
        if mode == "keyword" or not embeddings_on:
            results = search_service.search_files(query, current_user, drive=drive)
            mode = "keyword"
        else:
            if mode == "auto":
                mode = "hybrid"
            adv = search_service.search_advanced(
                current_user, query, limit=50, mode=mode, drive=drive)
            ids = [r["file_id"] for r in adv["results"]]
            files = {f.id: f for f in StoredFile.query.filter(
                StoredFile.id.in_(ids)).all()} if ids else {}
            results = []
            for r in adv["results"]:
                stored = files.get(r["file_id"])
                if stored is None:
                    continue
                index = stored.index
                results.append({
                    "file": stored,
                    "name_html": escape(r["name"]),
                    "matches": [],
                    "snippet": r.get("snippet") or "",
                    "caption": index.caption if index else None,
                    "tags": hashtag_service.get_tags(index),
                })
    else:
        results = []
        embeddings_on = embedding_service.embeddings_available(current_user)
        mode = "keyword"
    return render_template("search/results.html", query=query, results=results,
                           embeddings_on=embeddings_on, mode=mode)


@bp.route("/api/search")
@login_required
def api_search():
    """Instant-search endpoint for the search dock dropdown."""
    query = request.args.get("q", "").strip()
    if query:
        drive = drive_service.get_current_drive(current_user)
        results = search_service.search_files(query, current_user, limit=8,
                                              drive=drive)
    else:
        results = []
    return jsonify([
        {
            "file_id": r["file"].id,
            "name": r["file"].name,
            "name_html": str(r["name_html"]),
            "extension": r["file"].extension,
            "is_image": r["file"].is_image,
            "snippet": str(r["snippet"]),
        }
        for r in results
    ])


@bp.route("/api/drives")
@login_required
def api_drives():
    """Drive list for the AI chat's $ mention picker."""
    drives = (Drive.query
              .filter_by(user_id=current_user.id)
              .order_by(Drive.name)
              .all())
    return jsonify([{"id": d.id, "name": d.name, "is_synced": d.is_synced}
                    for d in drives])
