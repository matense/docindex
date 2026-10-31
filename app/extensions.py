from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()

login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."


@login_manager.unauthorized_handler
def _unauthorized():
    """JSON clients get a 401 envelope, browsers keep the login redirect."""
    from flask import jsonify, redirect, request, url_for

    from app.utils import wants_json

    if wants_json(request):
        return jsonify({"error": "Authentication required."}), 401
    return redirect(url_for("auth.login", next=request.path))
login_manager.session_protection = "strong"
