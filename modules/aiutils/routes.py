"""AI Utils reference page: the tool catalog the assistant can call.

Admins can switch whole sections or individual tools off here — disabled
tools are hidden from everyone else on this page and no longer offered to
the model (agent_service._active_tools filters them out).
"""

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user

from app.services import module_service

from .tools import TOOL_SPECS

bp = Blueprint("aiutils", __name__, template_folder="templates")


@bp.get("/")
def index():
    toggles = module_service.get_tool_toggles("aiutils")
    off_sections = set(toggles["sections"])
    off_tools = set(toggles["tools"])
    is_admin = bool(getattr(current_user, "is_admin", False))
    by_category = {}
    for spec in TOOL_SPECS:
        section_off = spec["category"] in off_sections
        tool_off = spec["name"] in off_tools or section_off
        # Non-admins see the usable catalog only; admins see everything
        # with switches (a disabled section still shows, grayed out).
        if tool_off and not is_admin:
            continue
        by_category.setdefault(spec["category"], []).append({
            "name": spec["name"],
            "label": spec["label"],
            "example": spec["example"],
            "description": spec["definition"]["function"]["description"],
            "enabled": not tool_off,
            "section_enabled": not section_off,
        })
    return render_template("aiutils/index.html", groups=by_category,
                           is_admin=is_admin)


@bp.post("/toggles")
def save_toggles():
    if not getattr(current_user, "is_admin", False):
        abort(403)
    data = request.get_json(silent=True) or {}
    saved = module_service.set_tool_toggles(
        "aiutils", data.get("sections"), data.get("tools"))
    return jsonify({"ok": True, "disabled": saved})
