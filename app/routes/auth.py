from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import Setting, User
from ..utils import json_error, wants_json

bp = Blueprint("auth", __name__)


def _public_user(user):
    return {"id": user.id, "username": user.username,
            "email": user.email, "is_admin": bool(user.is_admin)}


@bp.route("/api/me")
@login_required
def me():
    """Who is logged in (React shell bootstrap). Always JSON."""
    return jsonify({"user": _public_user(current_user._get_current_object())})


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        if wants_json():
            return jsonify({"ok": True,
                            "user": _public_user(
                                current_user._get_current_object())})
        return redirect(url_for("search.results"))
    if request.method == "POST":
        if request.is_json:
            data = request.get_json(silent=True) or {}
            username = str(data.get("username", "")).strip()
            password = data.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user)
                return jsonify({"ok": True, "user": _public_user(user)})
            return json_error("Invalid username or password.", 401)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            # Open-redirect guard: only allow relative paths (no //, no scheme).
            next_page = request.args.get("next") or ""
            if not next_page.startswith("/") or next_page.startswith("//"):
                next_page = ""
            return redirect(next_page or url_for("search.results"))
        flash("Invalid username or password.", "error")
    return render_template("auth/login.html",
                           registration_enabled=Setting.get_bool("registration_enabled", True))


def _validate_register(username, email, password, confirm):
    """Shared signup validation for the form and JSON twins.

    Returns the error message, or None when the payload is valid.
    (Uniqueness race is still handled at commit time by the caller.)
    """
    if not username or not email or not password:
        return "All fields are required."
    if len(username) < 3 or len(username) > 64:
        return "Username must be 3-64 characters."
    if "@" not in email or "." not in email or len(email) > 120:
        return "Enter a valid email address."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if password != confirm:
        return "Passwords do not match."
    if User.query.filter((User.username == username)
                         | (User.email == email)).first():
        return "Username or email already taken."
    return None


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        if wants_json():
            return jsonify({"ok": True,
                            "user": _public_user(
                                current_user._get_current_object())})
        return redirect(url_for("search.results"))
    if not Setting.get_bool("registration_enabled", True):
        if wants_json():
            return json_error("New registrations are currently disabled "
                              "by the administrator.", 403)
        flash("New registrations are currently disabled by the administrator.", "error")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if request.is_json:
            data = request.get_json(silent=True) or {}
            username = str(data.get("username", "")).strip()
            email = str(data.get("email", "")).strip()
            password = data.get("password", "")
            error = _validate_register(username, email, password,
                                       data.get("confirm", password))
            if error is None:
                from sqlalchemy.exc import IntegrityError
                user = User(username=username, email=email)
                user.set_password(password)
                db.session.add(user)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    error = "Username or email already taken."
            if error is not None:
                return json_error(error, 400)
            login_user(user)
            return jsonify({"ok": True, "user": _public_user(user)})
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not username or not email or not password:
            flash("All fields are required.", "error")
        elif len(username) < 3 or len(username) > 64:
            flash("Username must be 3-64 characters.", "error")
        elif "@" not in email or "." not in email or len(email) > 120:
            flash("Enter a valid email address.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or email already taken.", "error")
        else:
            from sqlalchemy.exc import IntegrityError
            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Username or email already taken.", "error")
                return render_template("auth/register.html")
            login_user(user)
            return redirect(url_for("search.results"))
    return render_template("auth/register.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    if wants_json():
        return jsonify({"ok": True})
    return redirect(url_for("auth.login"))
