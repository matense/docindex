import csv
import io
import json
import os
from datetime import datetime

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..models import (
    AIConnection,
    ChatConversation,
    ChatMessage,
    Drive,
    ErrorLog,
    FileIndex,
    Folder,
    Setting,
    StoredFile,
    User,
)
from ..services import ai_service, indexing_service, module_service

bp = Blueprint("settings", __name__, url_prefix="/settings")

# Presets for well-known OpenAI-compatible providers.
PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "vision_model": "gpt-4o-mini",
        "embedding_model": "text-embedding-3-small",
    },
    "claude": {
        "label": "Claude (Anthropic)",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-5",
        "vision_model": "claude-sonnet-4-5",
        "embedding_model": "",
    },
    "kimi": {
        "label": "Kimi (Moonshot AI)",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2-0711-preview",
        "vision_model": "moonshot-v1-8k-vision-preview",
        "embedding_model": "",
    },
    "lmstudio": {
        "label": "LM Studio",
        "base_url": "http://localhost:1234/v1",
        "model": "",
        "vision_model": "",
        "embedding_model": "",
    },
    "ollama": {
        "label": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "llama3.1",
        "vision_model": "llava",
        "embedding_model": "nomic-embed-text",
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "base_url": "",
        "model": "",
        "vision_model": "",
        "embedding_model": "",
    },
}


def _get_owned(conn_id):
    conn = db.session.get(AIConnection, conn_id)
    if not conn or conn.user_id != current_user.id:
        return None
    return conn


def _valid_base_url(url):
    """Basic SSRF guard for AI base URLs: http(s) only, no private/link-local.

    Local Ollama/LM Studio (localhost) stays allowed — self-hosted users need
    it — but metadata-IP (169.254.x.x) and non-http schemes are rejected.
    """
    import ipaddress
    from urllib.parse import urlparse
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return False, "Invalid base URL."
    if p.scheme not in ("http", "https"):
        return False, "Base URL must start with http:// or https://."
    host = (p.hostname or "").lower()
    if not host:
        return False, "Base URL must include a host."
    # Block cloud metadata + non-http schemes; localhost stays allowed.
    if host in ("169.254.169.254", "metadata.google.internal"):
        return False, "That host is not allowed."
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False, "That IP address is not allowed."
        # Private LAN allowed only for http (local Ollama/LM Studio); never
        # exfiltrate keys to private hosts over plain http? keep permissive
        # for self-hosting but block metadata above.
    except ValueError:
        pass  # hostname, not IP — allow (DNS)
    return True, ""


def _clamp_int(value, lo, hi):
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, iv))


@bp.route("/help")
@login_required
def help_page():
    """User guide: every feature of the app, in one page."""
    return render_template("settings/help.html",
                           enabled_modules=module_service.help_entries())


@bp.route("/releases")
@login_required
def releases():
    """What's new: release notes for every published version."""
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "releases.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    return render_template("settings/releases.html",
                           releases=data.get("releases", []))


@bp.route("/profile")
@login_required
def profile():
    """User profile: account details, per-drive stats and AI usage.

    Per-drive word totals are NOT computed here — aggregating
    FileIndex.word_count costs ~300ms on large drives, so the page fills
    them in asynchronously from /settings/profile/word-stats."""
    drive_stats = []
    drives = (Drive.query.filter_by(user_id=current_user.id)
              .order_by(Drive.created_at).all())
    drive_ids = [d.id for d in drives]
    file_rows = (db.session.query(StoredFile.drive_id,
                                  func.count(StoredFile.id),
                                  func.coalesce(func.sum(StoredFile.size), 0))
                 .filter(StoredFile.user_id == current_user.id,
                         StoredFile.drive_id.in_(drive_ids or [-1]),
                         StoredFile.deleted_at.is_(None))
                 .group_by(StoredFile.drive_id).all()) if drive_ids else []
    file_stats = {drive_id: (count, size) for drive_id, count, size in file_rows}
    folder_stats = dict(
        db.session.query(Folder.drive_id, func.count(Folder.id))
        .filter(Folder.user_id == current_user.id,
                Folder.drive_id.in_(drive_ids or [-1]))
        .group_by(Folder.drive_id).all()) if drive_ids else {}
    for d in drives:
        file_count, total_size = file_stats.get(d.id, (0, 0))
        drive_stats.append({
            "drive": d,
            "file_count": file_count,
            "folder_count": folder_stats.get(d.id, 0),
            "total_size": total_size,
            "sync_stats": (json.loads(d.last_sync_stats)
                           if d.last_sync_stats else None),
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
                           trash_count=(StoredFile.query
                                        .filter(StoredFile.user_id == current_user.id,
                                                StoredFile.deleted_at.isnot(None))
                                        .count()),
                           error_count=ErrorLog.query.filter_by(level="error").count(),
                           registration_enabled=Setting.get_bool("registration_enabled", True))


@bp.route("/profile/word-stats")
@login_required
def profile_word_stats():
    """Per-drive indexed-word totals as JSON {drive_id: words}.

    One grouped pass over file_index; fetched lazily by the profile page
    because it is the only expensive stat there."""
    rows = (db.session.query(StoredFile.drive_id,
                             func.coalesce(func.sum(FileIndex.word_count), 0))
            .join(FileIndex, FileIndex.file_id == StoredFile.id)
            .filter(StoredFile.user_id == current_user.id,
                    StoredFile.deleted_at.is_(None))
            .group_by(StoredFile.drive_id).all())
    return jsonify({str(drive_id): int(words) for drive_id, words in rows})


# --------------------------------------------------------------------------
# Trash bin
# --------------------------------------------------------------------------

TRASH_PER_PAGE = 50


@bp.route("/trash")
@login_required
def trash():
    """Dedicated trash bin page: browse, search, restore or purge deleted
    files (linked from a card on the profile page)."""
    from ..services import drive_service
    page = max(1, request.args.get("page", 1, type=int))
    q = (request.args.get("q") or "").strip()
    query = (StoredFile.query
             .filter(StoredFile.user_id == current_user.id,
                     StoredFile.deleted_at.isnot(None)))
    if q:
        query = query.filter(StoredFile.name.ilike(f"%{q}%"))
    pagination = (query.order_by(StoredFile.deleted_at.desc())
                  .paginate(page=page, per_page=TRASH_PER_PAGE, error_out=False))
    total = (StoredFile.query
             .filter(StoredFile.user_id == current_user.id,
                     StoredFile.deleted_at.isnot(None))
             .count())
    drives = drive_service.trashed_drives(current_user)
    return render_template("settings/trash.html", files=pagination.items,
                           pagination=pagination, q=q, total=total,
                           drives=drives,
                           auto_purge=current_user.trash_auto_purge)


@bp.route("/trash/settings", methods=["POST"])
@login_required
def trash_settings():
    """Toggle the 30-day automatic purge (off = trash waits forever)."""
    current_user.trash_auto_purge = request.form.get("trash_auto_purge") == "on"
    db.session.commit()
    if current_user.trash_auto_purge:
        flash("Automatic purge enabled: trash older than 30 days is "
              "permanently deleted, including whole drives.", "success")
    else:
        flash("Automatic purge disabled: trashed items wait until you "
              "delete them manually.", "success")
    return redirect(url_for("settings.trash"))


# --------------------------------------------------------------------------
# Error logs (admin)
# --------------------------------------------------------------------------

LOGS_PER_PAGE = 50


def _filtered_logs(args):
    """ErrorLog query (newest first) with the page's level/source/text filters."""
    level = args.get("level", "")
    source = args.get("source", "")
    q = (args.get("q") or "").strip()
    query = ErrorLog.query
    if level in ("error", "warning"):
        query = query.filter_by(level=level)
    if source:
        query = query.filter_by(source=source)
    if q:
        query = query.filter(ErrorLog.message.ilike(f"%{q}%"))
    return query.order_by(ErrorLog.id.desc()), level, source, q


@bp.route("/logs")
@login_required
def logs():
    """Central error/warning log page (admin only)."""
    if not current_user.is_admin:
        abort(403)
    page = max(1, request.args.get("page", 1, type=int))
    query, level, source, q = _filtered_logs(request.args)
    pagination = query.paginate(page=page, per_page=LOGS_PER_PAGE,
                                error_out=False)
    sources = [s for (s,) in (db.session.query(ErrorLog.source)
                              .distinct().order_by(ErrorLog.source).all())]
    return render_template("settings/logs.html", logs=pagination.items,
                           pagination=pagination, level=level, source=source,
                           q=q, sources=sources)


@bp.route("/logs/export")
@login_required
def logs_export():
    """Download the filtered log as CSV (admin only). Honors the page's
    level/source/text filters and exports ALL matching rows (no pagination)."""
    if not current_user.is_admin:
        abort(403)
    query, _level, _source, _q = _filtered_logs(request.args)

    def _safe(v):
        s = "" if v is None else str(v)
        # CSV formula injection: Excel executes =,+,-,@ prefixes.
        if s[:1] in ("=", "+", "-", "@"):
            s = "'" + s
        return s

    def _generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "created_at", "level", "source", "path",
                         "user_id", "message", "detail"])
        yield buf.getvalue()
        # Stream in chunks (no unbounded .all() OOM on 100k rows).
        for log in query.yield_per(500):
            buf.seek(0)
            buf.truncate(0)
            writer.writerow([
                log.id,
                log.created_at.isoformat() if log.created_at else "",
                log.level, log.source, _safe(log.path), log.user_id or "",
                _safe(log.message), _safe(log.detail or ""),
            ])
            yield buf.getvalue()
    from flask import stream_with_context
    filename = "docindex-error-logs-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".csv"
    return Response(stream_with_context(_generate()), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.route("/logs/clear", methods=["POST"])
@login_required
def logs_clear():
    if not current_user.is_admin:
        abort(403)
    ErrorLog.query.delete(synchronize_session=False)
    db.session.commit()
    flash("Error log cleared.", "success")
    return redirect(url_for("settings.logs"))


@bp.route("/index-queue/status")
@login_required
def index_queue_status():
    """JSON snapshot of the user's indexing queue (polled by the profile card)."""
    return jsonify(indexing_service.get_queue_status(current_user))


@bp.route("/index-queue/pause", methods=["POST"])
@login_required
def index_queue_pause():
    """Pause the (shared) indexing queue — pending jobs stay in the DB."""
    indexing_service.pause_queue()
    return jsonify({"ok": True, "paused": True})


@bp.route("/index-queue/resume", methods=["POST"])
@login_required
def index_queue_resume():
    indexing_service.resume_queue()
    return jsonify({"ok": True, "paused": False})


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
    elif len(new) < 8:
        flash("Password must be at least 8 characters.", "error")
    elif new != confirm:
        flash("New passwords do not match.", "error")
    else:
        current_user.set_password(new)
        db.session.commit()
        flash("Password changed.", "success")
    return redirect(url_for("settings.profile"))


@bp.route("/profile/theme", methods=["POST"])
@login_required
def profile_theme():
    """Per-user UI theme: "light" (default) or "dark"."""
    theme = request.form.get("theme", "light")
    if theme not in ("light", "dark"):
        theme = "light"
    current_user.theme = theme
    db.session.commit()
    flash(f"Theme set to {theme}.", "success")
    return redirect(url_for("settings.profile"))


@bp.route("/profile/background", methods=["POST"])
@login_required
def profile_background():
    """Static background (no animation) — easier on screen-sharing codecs."""
    current_user.static_background = request.form.get("enabled") == "on"
    db.session.commit()
    flash(("Static background on — the animated background is disabled."
           if current_user.static_background else
           "Static background off — the animated background is back."),
          "success")
    return redirect(url_for("settings.profile"))


@bp.route("/profile/registration", methods=["POST"])
@login_required
def profile_registration():
    """Admin-only: enable/disable new user registrations."""
    if not current_user.is_admin:
        abort(403)
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
    if not base_url or not model:
        flash("Base URL and model are required.", "error")
        return redirect(url_for("settings.ai_settings", edit=conn_id))
    ok, msg = _valid_base_url(base_url)
    if not ok:
        flash(msg, "error")
        return redirect(url_for("settings.ai_settings", edit=conn_id))
    if not name:
        name = model  # name is optional: fall back to the model name

    conn.name = name[:120]
    conn.base_url = base_url
    conn.model = model[:120]
    conn.vision_model = request.form.get("vision_model", "").strip()[:120]
    conn.embedding_model = request.form.get("embedding_model", "").strip()[:120]
    conn.max_steps = _clamp_int(request.form.get("max_steps", type=int), 1, 128)
    # 0 = unlimited; negatives would brick the connection (never a free slot).
    raw_rpm = request.form.get("rate_limit_rpm", type=int)
    conn.rate_limit_rpm = None if raw_rpm is None else max(0, min(raw_rpm, 600))
    conn.history_messages = _clamp_int(
        request.form.get("history_messages", type=int), 1, 200)
    conn.max_prompt_tokens = _clamp_int(
        request.form.get("max_prompt_tokens", type=int), 1000, 500000)
    # Only replace the key if a new one was entered (empty keeps the old one)
    new_key = request.form.get("api_key", "").strip()
    if new_key:
        conn.api_key = ai_service.encrypt_api_key(new_key)
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

    if not base_url or not model:
        flash("Base URL and model are required.", "error")
        return redirect(url_for("settings.ai_settings"))
    ok, msg = _valid_base_url(base_url)
    if not ok:
        flash(msg, "error")
        return redirect(url_for("settings.ai_settings"))

    name = (name or model)[:120]  # name is optional: fall back to the model name
    is_first = AIConnection.query.filter_by(user_id=current_user.id).count() == 0
    raw_rpm = request.form.get("rate_limit_rpm", type=int)
    conn = AIConnection(
        user_id=current_user.id,
        name=name,
        base_url=base_url,
        api_key=ai_service.encrypt_api_key(api_key),
        model=model[:120],
        vision_model=vision_model[:120],
        embedding_model=request.form.get("embedding_model", "").strip()[:120],
        max_steps=_clamp_int(request.form.get("max_steps", type=int), 1, 128),
        rate_limit_rpm=None if raw_rpm is None else max(0, min(raw_rpm, 600)),
        history_messages=_clamp_int(
            request.form.get("history_messages", type=int), 1, 200),
        max_prompt_tokens=_clamp_int(
            request.form.get("max_prompt_tokens", type=int), 1000, 500000),
        is_active=is_first or bool(request.form.get("is_active")),
    )
    if conn.is_active:
        AIConnection.query.filter_by(user_id=current_user.id).update({"is_active": False})
    db.session.add(conn)
    db.session.commit()
    flash(f"Connection '{name}' saved.", "success")
    return redirect(url_for("settings.ai_settings"))


@bp.route("/ai/<int:conn_id>/clone", methods=["POST"])
@login_required
def ai_clone(conn_id):
    """Duplicate a connection (including its key) and open the copy for edit."""
    conn = _get_owned(conn_id)
    if not conn:
        flash("Connection not found.", "error")
        return redirect(url_for("settings.ai_settings"))
    copy = AIConnection(
        user_id=current_user.id,
        name=f"{conn.name} (copy)",
        base_url=conn.base_url,
        api_key=conn.api_key,
        model=conn.model,
        vision_model=conn.vision_model,
        embedding_model=conn.embedding_model,
        max_steps=conn.max_steps,
        rate_limit_rpm=conn.rate_limit_rpm,
        history_messages=conn.history_messages,
        max_prompt_tokens=conn.max_prompt_tokens,
        is_active=False,  # never steal the active flag
    )
    db.session.add(copy)
    db.session.commit()
    flash(f"Connection cloned as '{copy.name}'.", "success")
    return redirect(url_for("settings.ai_settings", edit=copy.id))


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
    ok, msg = _valid_base_url(base_url)
    if not ok:
        return jsonify({"ok": False, "message": msg}), 400
    ok, message = ai_service.test_connection(base_url, api_key)
    return jsonify({"ok": ok, "message": message})


@bp.route("/ai/<int:conn_id>/test", methods=["POST"])
@login_required
def ai_test_existing(conn_id):
    conn = _get_owned(conn_id)
    if not conn:
        return jsonify({"ok": False, "message": "Connection not found."}), 404
    ok, message = ai_service.test_connection(
        conn.base_url, ai_service.decrypt_api_key(conn.api_key or ""))
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
            api_key = ai_service.decrypt_api_key(conn.api_key or "")
    if not base_url:
        return jsonify({"ok": False, "message": "Base URL is required."}), 400
    ok_url, url_msg = _valid_base_url(base_url)
    if not ok_url:
        return jsonify({"ok": False, "message": url_msg}), 400
    try:
        ok, result = ai_service.list_models(base_url, api_key)
        if ok:
            return jsonify({"ok": True, "models": result})
        return jsonify({"ok": False, "message": result})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# --------------------------------------------------------------------------
# Modules (admin)
# --------------------------------------------------------------------------

@bp.route("/modules")
@login_required
def modules():
    """Module manager: discovered modules with enable/disable (admin only)."""
    if not current_user.is_admin:
        abort(403)
    return render_template("settings/modules.html",
                           modules=module_service.list_modules())


@bp.route("/modules/<name>/toggle", methods=["POST"])
@login_required
def module_toggle(name):
    if not current_user.is_admin:
        abort(403)
    enabled = request.form.get("enabled") == "1"
    if not module_service.set_enabled(name, enabled, current_user):
        flash("Unknown module.", "error")
    else:
        flash(f"Module '{name}' {'enabled' if enabled else 'disabled'}.",
              "success")
    return redirect(url_for("settings.modules"))
