"""Behaviors & personas — core management page.

Behaviors are user-made reusable AI instructions; personas are the bundled
read-only Agency library. Both can be enabled/disabled so pickers (chat
persona selector, Automation AI nodes' behavior field, AI tools) only show
what the user actually uses.
"""

from flask import (Blueprint, abort, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

from ..services import behavior_service, persona_service

bp = Blueprint("behaviors", __name__, url_prefix="/behaviors")


def _owned_behavior(behavior_id):
    behavior = behavior_service.get_behavior(behavior_id, current_user.id)
    if behavior is None:
        abort(404)
    return behavior


@bp.get("/")
@login_required
def index():
    items = behavior_service.list_behaviors(current_user.id)
    return render_template("behaviors/index.html", behaviors=items)


@bp.post("/create")
@login_required
def create():
    name = (request.form.get("name") or "").strip()[:120]
    content = (request.form.get("content") or "").strip()
    if not name or not content:
        abort(400, "A behavior needs a name and its instructions.")
    behavior_service.create_behavior(current_user.id, name, content)
    return redirect(url_for("behaviors.index"))


@bp.post("/<int:behavior_id>/update")
@login_required
def update(behavior_id):
    behavior = _owned_behavior(behavior_id)
    name = (request.form.get("name") or "").strip()[:120]
    content = (request.form.get("content") or "").strip()
    if not name or not content:
        abort(400, "A behavior needs a name and its instructions.")
    behavior_service.update_behavior(behavior, name, content)
    return redirect(url_for("behaviors.index"))


@bp.post("/<int:behavior_id>/delete")
@login_required
def delete(behavior_id):
    behavior_service.delete_behavior(_owned_behavior(behavior_id))
    return redirect(url_for("behaviors.index"))


@bp.post("/<int:behavior_id>/toggle")
@login_required
def toggle(behavior_id):
    behavior = _owned_behavior(behavior_id)
    data = request.get_json(silent=True) or {}
    enabled = behavior_service.set_enabled(
        behavior, bool(data.get("enabled")))
    return jsonify({"ok": True, "enabled": enabled})


@bp.post("/personas/<key>/toggle")
@login_required
def persona_toggle(key):
    data = request.get_json(silent=True) or {}
    enabled = persona_service.set_persona_enabled(
        current_user.id, key, bool(data.get("enabled")))
    if enabled is None:
        abort(404)
    return jsonify({"ok": True, "enabled": enabled})


@bp.post("/personas/division/<path:division>/toggle")
@login_required
def persona_division_toggle(division):
    """Enable/disable a whole persona division in one go."""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    count = persona_service.set_division_enabled(
        current_user.id, division, enabled)
    if count is None:
        abort(404)
    return jsonify({"ok": True, "enabled": enabled, "count": count})


@bp.get("/api/presets")
@login_required
def api_presets():
    """Enabled behaviors as preset options ({label, value=content}) — the
    Automation editor merges them into the AI nodes' behavior dropdown."""
    return jsonify({"behaviors": [
        {"label": b.name, "value": b.content}
        for b in behavior_service.list_behaviors(current_user.id,
                                                 enabled_only=True)]})
