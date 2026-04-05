from __future__ import annotations

import os
import re
import uuid
from datetime import date, timedelta
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


def room_creator_uid(room_data: dict[str, Any]) -> str | None:
    uid = room_data.get("created_by_uid")
    if uid:
        return str(uid)
    c = room_data.get("created_by")
    return str(c) if c else None


def room_has_any_bookings(room_id: str) -> bool:
    for day_doc in days_collection(room_id).stream():
        for _ in bookings_collection(room_id, day_doc.id).limit(1).stream():
            return True
    return False


def delete_room_cascade(room_id: str) -> None:
    for day_doc in days_collection(room_id).stream():
        day_id = day_doc.id
        for b in bookings_collection(room_id, day_id).stream():
            b.reference.delete()
        day_doc.reference.delete()
    rooms_collection().document(room_id).delete()


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
    room_snap = rooms_collection().document(room_id).get()
    if not room_snap.exists:
        return []
    room_name = str((room_snap.to_dict() or {}).get("name", ""))
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


_OCC_START_MIN = 9 * 60
_OCC_END_MIN = 18 * 60
_OCC_WINDOW_MIN = _OCC_END_MIN - _OCC_START_MIN


def _merge_intervals_list(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    cleaned = [(a, b) for a, b in intervals if a < b]
    if not cleaned:
        return []
    cleaned.sort(key=lambda x: x[0])
    out: list[tuple[int, int]] = [cleaned[0]]
    for s, e in cleaned[1:]:
        ps, pe = out[-1]
        if s <= pe:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


def merged_busy_intervals_window(room_id: str, day_id: str) -> list[tuple[int, int]]:
    """Bookings clipped to 09:00–18:00, merged (overlaps combined)."""
    intervals: list[tuple[int, int]] = []
    for b in bookings_collection(room_id, day_id).stream():
        d = b.to_dict() or {}
        try:
            sm = _time_to_minutes(str(d.get("start_time", "")))
            em = _time_to_minutes(str(d.get("end_time", "")))
        except ValueError:
            continue
        cs = max(sm, _OCC_START_MIN)
        ce = min(em, _OCC_END_MIN)
        if cs < ce:
            intervals.append((cs, ce))
    return _merge_intervals_list(intervals)


def occupied_minutes_in_business_window(room_id: str, day_id: str) -> int:
    merged = merged_busy_intervals_window(room_id, day_id)
    return sum(e - s for s, e in merged)


def _minutes_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def earliest_free_slot_next_5_days(room_id: str) -> str | None:
    today = date.today()
    for i in range(5):
        d = today + timedelta(days=i)
        ds = d.isoformat()
        busy = merged_busy_intervals_window(room_id, ds)
        cursor = _OCC_START_MIN
        for s, e in busy:
            if cursor < s:
                return f"{ds} · {_minutes_to_hhmm(cursor)}"
            cursor = max(cursor, e)
        if cursor < _OCC_END_MIN:
            return f"{ds} · {_minutes_to_hhmm(cursor)}"
    return None


def calendar_week_data(room_id: str) -> list[dict[str, Any]]:
    today = date.today()
    weekday_short = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    days: list[dict[str, Any]] = []
    for i in range(5):
        d = today + timedelta(days=i)
        ds = d.isoformat()
        segments: list[dict[str, Any]] = []
        for b in bookings_collection(room_id, ds).stream():
            data = b.to_dict() or {}
            try:
                sm = _time_to_minutes(str(data.get("start_time", "")))
                em = _time_to_minutes(str(data.get("end_time", "")))
            except ValueError:
                continue
            cs = max(sm, _OCC_START_MIN)
            ce = min(em, _OCC_END_MIN)
            if cs >= ce:
                continue
            top_pct = (cs - _OCC_START_MIN) / _OCC_WINDOW_MIN * 100.0
            height_pct = (ce - cs) / _OCC_WINDOW_MIN * 100.0
            segments.append(
                {
                    "top_pct": round(top_pct, 2),
                    "height_pct": max(round(height_pct, 2), 2.5),
                    "label": f"{_minutes_to_hhmm(cs)}–{_minutes_to_hhmm(ce)}",
                }
            )
        days.append(
            {
                "date": ds,
                "date_short": f"{d.strftime('%b')} {d.day}",
                "weekday": weekday_short[d.weekday()],
                "segments": segments,
            }
        )
    return days


def occupancy_percent_for_day(room_id: str, day_id: str) -> float:
    occ = occupied_minutes_in_business_window(room_id, day_id)
    if _OCC_WINDOW_MIN <= 0:
        return 0.0
    return min(100.0, round(100.0 * occ / _OCC_WINDOW_MIN, 1))


def next_five_day_occupancy_rows(room_id: str) -> list[dict[str, Any]]:
    today = date.today()
    rows: list[dict[str, Any]] = []
    for i in range(5):
        d = today + timedelta(days=i)
        ds = d.isoformat()
        rows.append(
            {
                "date": ds,
                "percent": occupancy_percent_for_day(room_id, ds),
            }
        )
    return rows


def get_all_bookings_on_day(day_id: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day_id):
        return []
    out: list[dict[str, Any]] = []
    for room_doc in rooms_collection().stream():
        rid = room_doc.id
        rname = str((room_doc.to_dict() or {}).get("name", ""))
        for b_doc in bookings_collection(rid, day_id).stream():
            data = b_doc.to_dict() or {}
            out.append(
                {
                    "id": b_doc.id,
                    "room_id": rid,
                    "room_name": rname,
                    "day_id": day_id,
                    "start_time": str(data.get("start_time", "")),
                    "end_time": str(data.get("end_time", "")),
                    "user_id": str(data.get("user_id", "")),
                }
            )
    out.sort(key=lambda x: (x["room_name"].lower(), x["start_time"]))
    return out


def collect_all_bookings_for_room(room_id: str) -> list[dict[str, Any]]:
    room_snap = rooms_collection().document(room_id).get()
    if not room_snap.exists:
        return []
    room_name = str((room_snap.to_dict() or {}).get("name", "Room"))
    out: list[dict[str, Any]] = []
    for day_doc in days_collection(room_id).stream():
        day_id = day_doc.id
        for b_doc in bookings_collection(room_id, day_id).stream():
            data = b_doc.to_dict() or {}
            out.append(
                {
                    "id": b_doc.id,
                    "room_id": room_id,
                    "room_name": room_name,
                    "day_id": day_id,
                    "start_time": str(data.get("start_time", "")),
                    "end_time": str(data.get("end_time", "")),
                    "user_id": str(data.get("user_id", "")),
                }
            )
    out.sort(key=lambda r: (r["day_id"], r["start_time"]))
    return out


def update_user_booking(
    uid: str,
    room_id: str,
    old_day_id: str,
    booking_id: str,
    new_day_id: str,
    start_t: str,
    end_t: str,
) -> tuple[bool, str]:
    ref = bookings_collection(room_id, old_day_id).document(booking_id)
    snap = ref.get()
    if not snap.exists:
        return False, "Booking not found."
    data = snap.to_dict() or {}
    if data.get("user_id") != uid:
        return False, "You can only edit your own bookings."
    if not rooms_collection().document(room_id).get().exists:
        return False, "That room no longer exists."
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", new_day_id):
        return False, "Invalid date."
    try:
        sm = _time_to_minutes(start_t)
        em = _time_to_minutes(end_t)
    except ValueError:
        return False, "Invalid start or end time."
    if em <= sm:
        return False, "End time must be after start time."

    start_norm = _normalize_time_display(start_t)
    end_norm = _normalize_time_display(end_t)

    if new_day_id == old_day_id:
        if booking_clashes_room_day(
            room_id, old_day_id, sm, em, exclude_booking_id=booking_id
        ):
            return False, "That time overlaps another booking for this room."
        ref.update({"start_time": start_norm, "end_time": end_norm})
        return True, ""

    if booking_clashes_room_day(room_id, new_day_id, sm, em, None):
        return False, "That time overlaps another booking for this room."

    payload: dict[str, Any] = {
        "user_id": uid,
        "start_time": start_norm,
        "end_time": end_norm,
    }
    ca = data.get("created_at")
    payload["created_at"] = ca if ca is not None else SERVER_TIMESTAMP

    ensure_day_document(room_id, new_day_id)
    new_ref = bookings_collection(room_id, new_day_id).document()
    batch = _firestore_client().batch()
    batch.set(new_ref, payload)
    batch.delete(ref)
    batch.commit()
    return True, ""


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
    uid = session.get("uid")
    room_docs: list[dict[str, Any]] = []
    try:
        for doc in rooms_collection().stream():
            data = doc.to_dict() or {}
            rid = doc.id
            creator = room_creator_uid(data)
            can_delete = False
            if uid and creator and creator == uid:
                can_delete = not room_has_any_bookings(rid)
            room_docs.append(
                {
                    "id": rid,
                    "name": data.get("name", ""),
                    "can_delete": can_delete,
                }
            )
    except Exception as ex:
        err = str(ex).strip() or type(ex).__name__
        flash(f"Could not load rooms: {err}{_firestore_auth_hint(err)}", "error")
    room_docs.sort(key=lambda r: str(r.get("name", "")).lower())

    bookings: list[dict[str, Any]] = []
    bookings_mode = (request.args.get("bookings_mode") or "").strip()
    bookings_room_id = (request.args.get("bookings_room_id") or "").strip()
    if uid:
        try:
            if bookings_mode == "all":
                bookings = get_user_bookings_all(uid)
            elif bookings_mode == "room" and bookings_room_id:
                bookings = get_user_bookings_for_room(uid, bookings_room_id)
        except Exception as ex:
            err = str(ex).strip() or type(ex).__name__
            flash(f"Could not load bookings: {err}{_firestore_auth_hint(err)}", "error")

    filter_day = (request.args.get("filter_day") or "").strip()
    day_bookings: list[dict[str, Any]] = []
    if filter_day:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", filter_day):
                day_bookings = get_all_bookings_on_day(filter_day)
            else:
                flash("Invalid day filter.", "error")
        except Exception as ex:
            err = str(ex).strip() or type(ex).__name__
            flash(f"Could not load day schedule: {err}{_firestore_auth_hint(err)}", "error")

    return render_template(
        "index.html",
        rooms=room_docs,
        bookings=bookings,
        bookings_mode=bookings_mode,
        bookings_room_id=bookings_room_id,
        filter_day=filter_day,
        day_bookings=day_bookings,
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


@main_bp.route("/rooms/<room_id>")
def room_detail(room_id: str):
    room_id = (room_id or "").strip()
    snap = rooms_collection().document(room_id).get()
    if not snap.exists:
        flash("Room not found.", "error")
        return redirect(url_for("main.index"))
    data = snap.to_dict() or {}
    room_name = str(data.get("name", "Room"))
    try:
        all_bookings = collect_all_bookings_for_room(room_id)
        occupancy_rows = next_five_day_occupancy_rows(room_id)
        earliest_free = earliest_free_slot_next_5_days(room_id)
        calendar_days = calendar_week_data(room_id)
    except Exception as ex:
        err = str(ex).strip() or type(ex).__name__
        flash(f"Could not load room: {err}{_firestore_auth_hint(err)}", "error")
        return redirect(url_for("main.index"))
    return render_template(
        "room_detail.html",
        room_id=room_id,
        room_name=room_name,
        bookings=all_bookings,
        occupancy_rows=occupancy_rows,
        earliest_free=earliest_free,
        calendar_days=calendar_days,
        current_uid=session.get("uid"),
    )


@main_bp.route("/rooms/<room_id>/delete", methods=["POST"])
@login_required
def delete_room(room_id: str):
    room_id = (room_id or "").strip()
    if not room_id:
        flash("Invalid room.", "error")
        return redirect(url_for("main.index"))
    ref = rooms_collection().document(room_id)
    snap = ref.get()
    if not snap.exists:
        flash("Room not found.", "error")
        return redirect(url_for("main.index"))
    data = snap.to_dict() or {}
    creator = room_creator_uid(data)
    if not creator or creator != session["uid"]:
        flash("You can only delete rooms you created.", "error")
        return redirect(url_for("main.index"))
    if room_has_any_bookings(room_id):
        flash("Cannot delete a room that still has bookings.", "error")
        return redirect(url_for("main.index"))
    delete_room_cascade(room_id)
    flash("Room deleted.", "success")
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
    "/bookings/<room_id>/<day_id>/<booking_id>/edit",
    methods=["GET", "POST"],
)
@login_required
def edit_booking(room_id: str, day_id: str, booking_id: str):
    ref = bookings_collection(room_id, day_id).document(booking_id)
    snap = ref.get()

    return_mode = (request.values.get("return_bookings_mode") or "").strip()
    return_rid = (request.values.get("return_bookings_room_id") or "").strip()

    if request.method == "GET":
        if not snap.exists:
            flash("Booking not found.", "error")
            return redirect(url_for("main.index"))
        data = snap.to_dict() or {}
        if data.get("user_id") != session["uid"]:
            flash("You can only edit your own bookings.", "error")
            return redirect(url_for("main.index"))
        room_snap = rooms_collection().document(room_id).get()
        room_name = (
            str((room_snap.to_dict() or {}).get("name", "Room"))
            if room_snap.exists
            else "Room"
        )
        if return_mode == "all":
            cancel_href = url_for("main.index") + "?" + urlencode({"bookings_mode": "all"})
        elif return_mode == "room" and return_rid:
            cancel_href = (
                url_for("main.index")
                + "?"
                + urlencode({"bookings_mode": "room", "bookings_room_id": return_rid})
            )
        else:
            cancel_href = url_for("main.index")
        return render_template(
            "edit_booking.html",
            room_id=room_id,
            room_name=room_name,
            day_id=day_id,
            booking_id=booking_id,
            start_time=str(data.get("start_time", "")),
            end_time=str(data.get("end_time", "")),
            return_bookings_mode=return_mode,
            return_bookings_room_id=return_rid,
            cancel_href=cancel_href,
        )

    new_day = (request.form.get("day") or "").strip()
    start_t = (request.form.get("start_time") or "").strip()
    end_t = (request.form.get("end_time") or "").strip()
    return_mode = (request.form.get("return_bookings_mode") or "").strip()
    return_rid = (request.form.get("return_bookings_room_id") or "").strip()

    ok, err_msg = update_user_booking(
        session["uid"], room_id, day_id, booking_id, new_day, start_t, end_t
    )
    if not ok:
        flash(err_msg, "error")
        q: dict[str, str] = {}
        if return_mode:
            q["return_bookings_mode"] = return_mode
        if return_rid:
            q["return_bookings_room_id"] = return_rid
        qs = ("?" + urlencode(q)) if q else ""
        return redirect(
            url_for(
                "main.edit_booking",
                room_id=room_id,
                day_id=day_id,
                booking_id=booking_id,
            )
            + qs
        )

    flash("Booking updated.", "success")
    if return_mode == "all":
        return redirect(
            url_for("main.index") + "?" + urlencode({"bookings_mode": "all"})
        )
    if return_mode == "room" and return_rid:
        return redirect(
            url_for("main.index")
            + "?"
            + urlencode({"bookings_mode": "room", "bookings_room_id": return_rid})
        )
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
