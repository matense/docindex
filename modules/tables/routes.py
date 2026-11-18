"""Tables module routes: list + view page skeleton (viewer itself is T2)."""

from flask import Blueprint
from flask_login import login_required

bp = Blueprint("tables", __name__,
               template_folder="templates", static_folder="static")


@bp.get("/")
@login_required
def index():
    # Skeleton only: T2 builds the real list/viewer UI.
    return "Tables module (viewer lands in T2).", 200


@bp.get("/<int:file_id>")
@login_required
def view(file_id):
    # Skeleton only: T2 builds the real table viewer page.
    return "Tables module (viewer lands in T2).", 200
