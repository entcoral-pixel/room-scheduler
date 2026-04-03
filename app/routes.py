from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urlencode

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app import auth
from app import db

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    rooms = db.list_rooms()
    bookings_mode = request.args.get("bookings_mode", "")
    selected_room_id = (request.args.get("bookings_room_id") or "").strip()
    current_uid = auth.current_user_id()
    bookings: list[dict[str, str]] = []

    if current_uid:
        if bookings_mode == "all":
            bookings = db.list_user_bookings(current_uid)
        elif bookings_mode == "room":
            if selected_room_id:
                bookings = db.list_user_bookings(current_uid, room_id=selected_room_id)
            else:
                flash("Choose a room to filter bookings.", "error")

    return render_template(
        "index.html",
        rooms=rooms,
        bookings=bookings,
        bookings_mode=bookings_mode,
        bookings_room_id=selected_room_id,
    )


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


@bp.post("/bookings")
@auth.login_required
def add_booking():
    room_id = (request.form.get("room_id") or "").strip()
    day_str = (request.form.get("day") or "").strip()
    start_time = (request.form.get("start_time") or "").strip()
    end_time = (request.form.get("end_time") or "").strip()

    if not room_id or not day_str or not start_time or not end_time:
        flash("Room, day, start time, and end time are required.", "error")
        return redirect(url_for("main.index"))

    if not db.room_exists(room_id):
        flash("Selected room does not exist.", "error")
        return redirect(url_for("main.index"))

    try:
        booking_day = date.fromisoformat(day_str)
        start_time_value = datetime.strptime(start_time, "%H:%M").time()
        end_time_value = datetime.strptime(end_time, "%H:%M").time()
    except ValueError:
        flash("Invalid date or time format.", "error")
        return redirect(url_for("main.index"))

    if start_time_value >= end_time_value:
        flash("End time must be after start time.", "error")
        return redirect(url_for("main.index"))

    uid = auth.current_user_id()
    if not uid:
        flash("You must be logged in.", "error")
        return redirect(url_for("main.login"))

    db.create_booking(
        room_id=room_id,
        day=booking_day,
        start_time=start_time,
        end_time=end_time,
        user_id=uid,
    )
    flash("Booking added.", "success")
    return redirect(url_for("main.index", bookings_mode="all"))


@bp.post("/bookings/<room_id>/<day_id>/<booking_id>/delete")
@auth.login_required
def delete_booking(room_id: str, day_id: str, booking_id: str):
    uid = auth.current_user_id()
    if not uid:
        flash("You must be logged in.", "error")
        return redirect(url_for("main.login"))

    try:
        deleted = db.delete_booking_for_user(
            room_id=room_id,
            day_id=day_id,
            booking_id=booking_id,
            user_id=uid,
        )
    except PermissionError as e:
        flash(str(e), "error")
        deleted = False

    if deleted:
        flash("Booking deleted.", "success")
    else:
        flash("Booking not found.", "error")

    bookings_mode = (request.form.get("bookings_mode") or "all").strip()
    bookings_room_id = (request.form.get("bookings_room_id") or "").strip()
    query: dict[str, str] = {"bookings_mode": bookings_mode}
    if bookings_mode == "room" and bookings_room_id:
        query["bookings_room_id"] = bookings_room_id
    return redirect(f"{url_for('main.index')}?{urlencode(query)}")
