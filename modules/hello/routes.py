from flask import Blueprint, render_template

bp = Blueprint("hello", __name__, template_folder="templates")


@bp.get("/")
def index():
    from flask_login import current_user

    from .models import HelloNote

    notes = (HelloNote.query
             .filter_by(user_id=current_user.id)
             .order_by(HelloNote.created_at.desc()).limit(50).all())
    return render_template("hello/index.html", notes=notes)
