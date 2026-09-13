from flask import Blueprint, render_template

bp = Blueprint("hello", __name__, template_folder="templates")


@bp.get("/")
def index():
    from .models import HelloNote

    notes = HelloNote.query.order_by(HelloNote.created_at.desc()).limit(50).all()
    return render_template("hello/index.html", notes=notes)
