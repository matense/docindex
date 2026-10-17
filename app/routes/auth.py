from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import Setting, User

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("search.results"))
    if request.method == "POST":
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


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("search.results"))
    if not Setting.get_bool("registration_enabled", True):
        flash("New registrations are currently disabled by the administrator.", "error")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
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
    return redirect(url_for("auth.login"))
