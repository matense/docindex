"""Dashboard module routes (stub — Task 2; full pages land in later tasks)."""

from flask import Blueprint

bp = Blueprint("dashboards", __name__,
               template_folder="templates", static_folder="static")


@bp.get("/")
def index():
    return "Dashboards", 200
