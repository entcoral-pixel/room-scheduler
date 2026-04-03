from __future__ import annotations

import os

from flask import Flask

from app import auth
from app.routes import bp as main_bp


def create_app() -> Flask:
    app = Flask(__name__, static_folder="../static", template_folder="../templates")
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

    auth.init_firebase()

    app.register_blueprint(main_bp)
    return app
