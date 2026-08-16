from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import (AIConnection, ChatConversation, ChatMessage, Drive,
                      FileIndex, Folder, Setting, StoredFile, User)
from ..services import ai_service

bp = Blueprint("settings", __name__, url_prefix="/settings")

# Presets for well-known OpenAI-compatible providers.
PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
    },
    "claude": {
        "label": "Claude (Anthropic)",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-5",
        "vision_model": "claude-sonnet-4-5",
    },
    "kimi": {
        "label": "Kimi (Moonshot AI)",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2-0711-preview",
        "vision_model": "moonshot-v1-8k-vision-preview",
    },
    "lmstudio": {
        "label": "LM Studio",
        "base_url": "http://localhost:1234/v1",
        "model": "",
        "vision_model": "",
    },
    "ollama": {
        "label": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.1",
        "vision_model": "llava",
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "base_url": "",
        "model": "",
        "vision_model": "",
    },
}


def _get_owned(conn_id):
    conn = db.session.get(AIConnection, conn_id)
    if not conn or conn.user_id != current_user.id:
        return None
    return conn


@bp.route("/profile")
@login_required
def profile():
    """User profile: account details, per-drive stats and AI usage."""
    drive_stats = []
    drives = (Drive.query.filter_by(user_id=current_user.id)
              .order_by(Drive.created_at).all())
    for d in drives:
        file_count, total_size = (db.session.query(
            func.count(StoredFile.id),
            func.coalesce(func.sum(StoredFile.size), 0))
            .filter_by(drive_id=d.id).first())
        total_words = (db.session.query(func.coalesce(func.sum(FileIndex.word_count), 0))
                       .join(StoredFile, FileIndex.file_id == StoredFile.id)
                       .filter(StoredFile.drive_id == d.id).scalar())
        drive_stats.append({
            "drive": d,
            "file_count": file_count,
            "folder_count": Folder.query.filter_by(drive_id=d.id).count(),
            "total_size": total_size,
            "total_words": total_words,
        })

    ai_stats = {
        "conversations": ChatConversation.query.filter_by(user_id=current_user.id).count(),
        "messages": (db.session.query(func.count(ChatMessage.id))
                     .join(ChatConversation)
                     .filter(ChatConversation.user_id == current_user.id).scalar()),
        "active_connection": AIConnection.query.filter_by(user_id=current_user.id,
                                                          is_active=True).first(),
    }
    return render_template("settings/profile.html",
                           drive_stats=drive_stats,
                           ai_stats=ai_stats,
                           registration_enabled=Setting.get_bool("registration_enabled", True))


@bp.route("/profile/email", methods=["POST"])
@login_required
def profile_email():
    email = request.form.get("email", "").strip()
    if not email:
        flash("Email is required.", "error")
    elif User.query.filter(User.email == email, User.id != current_user.id).first():
        flash("That email is already in use.", "error")
    else:
        current_user.email = email
        db.session.commit()
        flash("Email updated.", "success")
    return redirect(url_for("settings.profile"))


@bp.route("/profile/password", methods=["POST"])
@login_required
def profile_password():
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not current_user.check_password(current):
        flash("Current password is incorrect.", "error")
    elif not new:
        flash("New password is required.", "error")
    elif new != confirm:
        flash("New passwords do not match.", "error")
    else:
        current_user.set_password(new)
        db.session.commit()
        flash("Password changed.", "success")
    return redirect(url_for("settings.profile"))


@bp.route("/profile/registration", methods=["POST"])
@login_required
def profile_registration():
    """Admin-only: enable/disable new user registrations."""
    if not current_user.is_admin:
        flash("Only administrators can change this setting.", "error")
        return redirect(url_for("settings.profile"))
    enabled = bool(request.form.get("enabled"))
    Setting.set("registration_enabled", "1" if enabled else "0")
    flash(f"New registrations {'enabled' if enabled else 'disabled'}.", "success")
    return redirect(url_for("settings.profile"))


@bp.route("/ai")
@login_required
def ai_settings():
    connections = (AIConnection.query
                   .filter_by(user_id=current_user.id)
                   .order_by(AIConnection.created_at).all())
    editing = None
    edit_id = request.args.get("edit", type=int)
    if edit_id:
        editing = _get_owned(edit_id)
    return render_template("settings/ai.html", connections=connections,
                           providers=PROVIDERS, editing=editing)


@bp.route("/ai/<int:conn_id>/edit", methods=["POST"])
@login_required
def ai_edit(conn_id):
    conn = _get_owned(conn_id)
    if not conn:
        flash("Connection not found.", "error")
        return redirect(url_for("settings.ai_settings"))

    name = request.form.get("name", "").strip()
    base_url = request.form.get("base_url", "").strip()
    model = request.form.get("model", "").strip()
    if not name or not base_url or not model:
        flash("Name, base URL and model are required.", "error")
        return redirect(url_for("settings.ai_settings", edit=conn_id))

    conn.name = name
    conn.base_url = base_url
    conn.model = model
    conn.vision_model = request.form.get("vision_model", "").strip()
    conn.max_steps = request.form.get("max_steps", type=int) or None
    # Only replace the key if a new one was entered (empty keeps the old one)
    new_key = request.form.get("api_key", "").strip()
    if new_key:
        conn.api_key = new_key
    if request.form.get("is_active"):
        AIConnection.query.filter_by(user_id=current_user.id).update({"is_active": False})
        conn.is_active = True
    db.session.commit()
    flash(f"Connection '{conn.name}' updated.", "success")
    return redirect(url_for("settings.ai_settings"))


@bp.route("/ai/add", methods=["POST"])
@login_required
def ai_add():
    name = request.form.get("name", "").strip()
    base_url = request.form.get("base_url", "").strip()
    model = request.form.get("model", "").strip()
    vision_model = request.form.get("vision_model", "").strip()
    api_key = request.form.get("api_key", "").strip()

    if not name or not base_url or not model:
        flash("Name, base URL and model are required.", "error")
        return redirect(url_for("settings.ai_settings"))

    is_first = AIConnection.query.filter_by(user_id=current_user.id).count() == 0
    conn = AIConnection(
        user_id=current_user.id,
        name=name,
        base_url=base_url,
        api_key=api_key,
        model=model,
        vision_model=vision_model,
        max_steps=request.form.get("max_steps", type=int) or None,
        is_active=is_first or bool(request.form.get("is_active")),
    )
    if conn.is_active:
        AIConnection.query.filter_by(user_id=current_user.id).update({"is_active": False})
    db.session.add(conn)
    db.session.commit()
    flash(f"Connection '{name}' saved.", "success")
    return redirect(url_for("settings.ai_settings"))


@bp.route("/ai/<int:conn_id>/activate", methods=["POST"])
@login_required
def ai_activate(conn_id):
    conn = _get_owned(conn_id)
    if not conn:
        return redirect(url_for("settings.ai_settings"))
    AIConnection.query.filter_by(user_id=current_user.id).update({"is_active": False})
    conn.is_active = True
    db.session.commit()
    flash(f"'{conn.name}' is now active.", "success")
    return redirect(url_for("settings.ai_settings"))


@bp.route("/ai/<int:conn_id>/delete", methods=["POST"])
@login_required
def ai_delete(conn_id):
    conn = _get_owned(conn_id)
    if conn:
        db.session.delete(conn)
        db.session.commit()
        flash(f"Connection '{conn.name}' deleted.", "success")
    return redirect(url_for("settings.ai_settings"))


@bp.route("/ai/test", methods=["POST"])
@login_required
def ai_test():
    data = request.get_json(silent=True) or {}
    base_url = (data.get("base_url") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    if not base_url:
        return jsonify({"ok": False, "message": "Base URL is required."}), 400
    ok, message = ai_service.test_connection(base_url, api_key)
    return jsonify({"ok": ok, "message": message})


@bp.route("/ai/<int:conn_id>/test", methods=["POST"])
@login_required
def ai_test_existing(conn_id):
    conn = _get_owned(conn_id)
    if not conn:
        return jsonify({"ok": False, "message": "Connection not found."}), 404
    ok, message = ai_service.test_connection(conn.base_url, conn.api_key)
    return jsonify({"ok": ok, "message": message})


@bp.route("/ai/models", methods=["POST"])
@login_required
def ai_models():
    """Fetch the provider's model list for the dropdowns."""
    data = request.get_json(silent=True) or {}
    base_url = (data.get("base_url") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    # When editing without re-entering the key, use the stored one.
    conn_id = data.get("conn_id")
    if not api_key and conn_id:
        conn = _get_owned(conn_id)
        if conn:
            api_key = conn.api_key
    if not base_url:
        return jsonify({"ok": False, "message": "Base URL is required."}), 400
    try:
        ok, result = ai_service.list_models(base_url, api_key)
        if ok:
            return jsonify({"ok": True, "models": result})
        return jsonify({"ok": False, "message": result})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
