"""AI Utils reference page: the tool catalog the assistant can call."""

from flask import Blueprint, render_template

from .tools import TOOL_SPECS

bp = Blueprint("aiutils", __name__, template_folder="templates")


@bp.get("/")
def index():
    by_category = {}
    for spec in TOOL_SPECS:
        by_category.setdefault(spec["category"], []).append({
            "name": spec["name"],
            "label": spec["label"],
            "example": spec["example"],
            "description": spec["definition"]["function"]["description"],
        })
    return render_template("aiutils/index.html", groups=by_category)
