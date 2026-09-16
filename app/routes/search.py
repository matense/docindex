from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from ..models import Drive
from ..services import drive_service, search_service

bp = Blueprint("search", __name__)


@bp.route("/search")
@login_required
def results():
    if request.args.get("mode") == "ai":
        return render_template("search/ai.html")
    query = request.args.get("q", "").strip()
    if query:
        drive = drive_service.get_current_drive(current_user)
        results = search_service.search_files(query, current_user, drive=drive)
    else:
        results = []
    return render_template("search/results.html", query=query, results=results)


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
