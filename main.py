from __future__ import annotations

import json
import os
import re
import uuid
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from dotenv import load_dotenv

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from firebase_admin import firestore as firebase_firestore
from flask import Blueprint, Flask, flash, redirect, render_template, request, session, url_for
from google.cloud import firestore
from google.cloud.firestore import SERVER_TIMESTAMP

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

FIRESTORE_DATABASE_ID = "a1-0000000"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "soqiqi")


def _init_firebase() -> None:
    if firebase_admin._apps:
        return
    path = os.path.join(os.path.dirname(__file__), "service-account.json")
    if os.path.isfile(path):
        firebase_admin.initialize_app(credentials.Certificate(path))
    else:
        firebase_admin.initialize_app()


def _firestore_database_id() -> str:
    return FIRESTORE_DATABASE_ID


def _firestore_client() -> firestore.Client:
    
    return firebase_firestore.client(database_id=_firestore_database_id())


_init_firebase()


def _firestore_auth_hint(message: str) -> str:
    m = message.lower()
    if "invalid jwt" in m or "invalid_grant" in m:
        return (
            " Fix: generate a new key in Firebase Console → Project settings → Service accounts "
            "(or Google Cloud → IAM → Service accounts), replace service-account.json, and do not "
            "hand-edit the file—the private_key field must keep \\n line breaks inside the JSON."
        )
    return ""


def rooms_collection():
    return _firestore_client().collection("rooms")


def days_collection(room_id: str):
    return rooms_collection().document(room_id).collection("days")


def bookings_collection(room_id: str, day_id: str):
    return days_collection(room_id).document(day_id).collection("bookings")


def normalize_room_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def room_name_exists(normalized: str) -> bool:
    target = normalized.casefold()
    for doc in rooms_collection().stream():
        data = doc.to_dict() or {}
        if str(data.get("name", "")).strip().casefold() == target:
            return True
    return False


def _time_to_minutes(t: str) -> int:
    parts = (t or "").strip().split(":")
    if len(parts) < 2:
        raise ValueError("Invalid time")
    h, m = int(parts[0]), int(parts[1])
    return h * 60 + m


def _normalize_time_display(t: str) -> str:
    m = _time_to_minutes(t)
    return f"{m // 60:02d}:{m % 60:02d}"


def intervals_overlap_minutes(
    start_a: int, end_a: int, start_b: int, end_b: int
) -> bool:
    return start_a < end_b and start_b < end_a


def ensure_day_document(room_id: str, day_id: str) -> None:
    ref = days_collection(room_id).document(day_id)
    ref.set({"date": day_id}, merge=True)


def booking_clashes_room_day(
    room_id: str, day_id: str, start_m: int, end_m: int, exclude_booking_id: str | None = None
) -> bool:
    for b in bookings_collection(room_id, day_id).stream():
        if exclude_booking_id and b.id == exclude_booking_id:
            continue
        d = b.to_dict() or {}
        try:
            os_m = _time_to_minutes(str(d.get("start_time", "")))
            oe_m = _time_to_minutes(str(d.get("end_time", "")))
        except ValueError:
            continue
        if intervals_overlap_minutes(start_m, end_m, os_m, oe_m):
            return True
    return False


def get_user_bookings_all(user_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for room_doc in rooms_collection().stream():
        room_id = room_doc.id
        room_name = str((room_doc.to_dict() or {}).get("name", ""))
        for day_doc in days_collection(room_id).stream():
            day_id = day_doc.id
            for b_doc in bookings_collection(room_id, day_id).stream():
                data = b_doc.to_dict() or {}
                if data.get("user_id") != user_id:
                    continue
                out.append(
                    {
                        "id": b_doc.id,
                        "room_id": room_id,
                        "room_name": room_name,
                        "day_id": day_id,
                        "start_time": str(data.get("start_time", "")),
                        "end_time": str(data.get("end_time", "")),
                    }
                )
    out.sort(key=lambda r: (r["day_id"], r["start_time"]))
    return out


def get_user_bookings_for_room(user_id: str, room_id: str) -> list[dict[str, Any]]:
    room_name = _get_room_name(room_id)
    if not room_name and not rooms_collection().document(room_id).get().exists:
        return []
    out: list[dict[str, Any]] = []
    for day_doc in days_collection(room_id).stream():
        day_id = day_doc.id
        for b_doc in bookings_collection(room_id, day_id).stream():
            data = b_doc.to_dict() or {}
            if data.get("user_id") != user_id:
                continue
            out.append(
                {
                    "id": b_doc.id,
                    "room_id": room_id,
                    "room_name": room_name or "Room",
                    "day_id": day_id,
                    "start_time": str(data.get("start_time", "")),
                    "end_time": str(data.get("end_time", "")),
                }
            )
    out.sort(key=lambda r: (r["day_id"], r["start_time"]))
    return out


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("uid"):
            flash("Please sign in to continue.", "error")
            return redirect(url_for("main.login"))
        return view(*args, **kwargs)

    return wrapped


def _bookings_index_redirect_args() -> str:
    mode = (request.form.get("return_bookings_mode") or "").strip()
    rid = (request.form.get("return_bookings_room_id") or "").strip()
    if mode == "all":
        return "?" + urlencode({"bookings_mode": "all"})
    if mode == "room" and rid:
        return "?" + urlencode({"bookings_mode": "room", "bookings_room_id": rid})
    return ""


@app.route("/auth/session", methods=["POST"])
def auth_session():
    id_token = (request.form.get("id_token") or "").strip()
    if not id_token:
        return {"error": "Missing id_token"}, 400
    try:
        decoded = firebase_auth.verify_id_token(id_token)
        uid = decoded.get("uid")
        if not uid:
            return {"error": "Invalid token"}, 401
        session["uid"] = uid
        session["email"] = decoded.get("email") or ""
        return {"ok": True}
    except Exception as ex:
        return {"error": str(ex) or "Invalid token"}, 401


@app.route("/auth/logout", methods=["GET"])
def auth_logout():
    session.clear()
    return redirect(url_for("main.index"))


main_bp = Blueprint("main", __name__)


@main_bp.route("/login")
def login():
    if session.get("uid"):
        return redirect(url_for("main.index"))
    return render_template("login.html")


@main_bp.route("/")
def index():
    room_docs: list[dict[str, Any]] = []
    try:
        for doc in rooms_collection().stream():
            data = doc.to_dict() or {}
            room_docs.append({"id": doc.id, "name": data.get("name", "")})
    except Exception as ex:
        err = str(ex).strip() or type(ex).__name__
        flash(f"Could not load rooms: {err}{_firestore_auth_hint(err)}", "error")
    room_docs.sort(key=lambda r: str(r.get("name", "")).lower())

    bookings: list[dict[str, Any]] = []
    bookings_mode = (request.args.get("bookings_mode") or "").strip()
    bookings_room_id = (request.args.get("bookings_room_id") or "").strip()
    uid = session.get("uid")
    if uid:
        try:
            if bookings_mode == "all":
                bookings = get_user_bookings_all(uid)
            elif bookings_mode == "room" and bookings_room_id:
                bookings = get_user_bookings_for_room(uid, bookings_room_id)
        except Exception as ex:
            err = str(ex).strip() or type(ex).__name__
            flash(f"Could not load bookings: {err}{_firestore_auth_hint(err)}", "error")

    return render_template(
        "index.html",
        rooms=room_docs,
        bookings=bookings,
        bookings_mode=bookings_mode,
        bookings_room_id=bookings_room_id,
    )


@main_bp.route("/rooms/add", methods=["POST"])
@login_required
def add_room():
    name = normalize_room_name(request.form.get("name", ""))
    if not name:
        flash("Room name is required.", "error")
        return redirect(url_for("main.index"))
    if len(name) > 200:
        flash("Room name is too long.", "error")
        return redirect(url_for("main.index"))
    if room_name_exists(name):
        flash("A room with that name already exists.", "error")
        return redirect(url_for("main.index"))
    room_id = uuid.uuid4().hex
    rooms_collection().document(room_id).set(
        {
            "name": name,
            "created_by_uid": session["uid"],
            "created_at": SERVER_TIMESTAMP,
        }
    )
    flash("Room added.", "success")
    return redirect(url_for("main.index"))


@main_bp.route("/bookings/add", methods=["POST"])
@login_required
def add_booking():
    room_id = (request.form.get("room_id") or "").strip()
    day = (request.form.get("day") or "").strip()
    start_t = (request.form.get("start_time") or "").strip()
    end_t = (request.form.get("end_time") or "").strip()

    if not room_id or not day or not start_t or not end_t:
        flash("Room, day, start time, and end time are required.", "error")
        return redirect(url_for("main.index"))

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        flash("Invalid date.", "error")
        return redirect(url_for("main.index"))

    if not rooms_collection().document(room_id).get().exists:
        flash("That room does not exist.", "error")
        return redirect(url_for("main.index"))

    try:
        sm = _time_to_minutes(start_t)
        em = _time_to_minutes(end_t)
    except ValueError:
        flash("Invalid start or end time.", "error")
        return redirect(url_for("main.index"))

    if em <= sm:
        flash("End time must be after start time.", "error")
        return redirect(url_for("main.index"))

    day_id = day
    if booking_clashes_room_day(room_id, day_id, sm, em):
        flash("That time overlaps another booking for this room.", "error")
        return redirect(url_for("main.index"))

    ensure_day_document(room_id, day_id)
    start_norm = _normalize_time_display(start_t)
    end_norm = _normalize_time_display(end_t)
    bookings_collection(room_id, day_id).add(
        {
            "user_id": session["uid"],
            "start_time": start_norm,
            "end_time": end_norm,
            "created_at": SERVER_TIMESTAMP,
        }
    )
    flash("Booking added.", "success")
    return redirect(url_for("main.index"))


@main_bp.route(
    "/bookings/<room_id>/<day_id>/<booking_id>/delete", methods=["POST"]
)
@login_required
def delete_booking(room_id: str, day_id: str, booking_id: str):
    ref = bookings_collection(room_id, day_id).document(booking_id)
    snap = ref.get()
    if not snap.exists:
        flash("Booking not found.", "error")
        return redirect(url_for("main.index") + _bookings_index_redirect_args())
    data = snap.to_dict() or {}
    if data.get("user_id") != session["uid"]:
        flash("You can only delete your own bookings.", "error")
        return redirect(url_for("main.index") + _bookings_index_redirect_args())
    ref.delete()
    flash("Booking deleted.", "success")
    return redirect(url_for("main.index") + _bookings_index_redirect_args())


app.register_blueprint(main_bp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
