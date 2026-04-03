from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app import auth
from app import db

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    rooms = db.list_rooms()
    return render_template("index.html", rooms=rooms)


@bp.route("/login")
def login():
    if auth.current_user_id():
        return redirect(url_for("main.index"))
    return render_template("login.html")


@bp.post("/auth/session")
def session_login():
    token = request.form.get("id_token")
    if not token and request.is_json:
        body = request.get_json(silent=True) or {}
        token = body.get("id_token")
    if not token:
        return {"error": "missing id_token"}, 400
    try:
        claims = auth.verify_id_token(token)
    except Exception:
        return {"error": "invalid token"}, 401
    session["uid"] = claims["uid"]
    session["email"] = claims.get("email", "")
    return {"ok": True}, 200


@bp.route("/auth/logout", methods=["GET", "POST"])
def session_logout():
    session.clear()
    return redirect(url_for("main.index"))


@bp.post("/rooms")
@auth.login_required
def add_room():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Room name is required.", "error")
        return redirect(url_for("main.index"))
    uid = auth.current_user_id()
    if not uid:
        flash("You must be logged in.", "error")
        return redirect(url_for("main.login"))
    try:
        db.create_room(name, uid)
        flash("Room added.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("main.index"))
