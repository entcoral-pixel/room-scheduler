from __future__ import annotations
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from google.cloud import firestore

_client: firestore.Client | None = None


def get_client() -> firestore.Client:
    global _client
    if _client is None:
        
        service_account_path = Path(__file__).resolve().parent.parent / "service-account.json"
        if not service_account_path.is_file():
            raise FileNotFoundError(
                f"Firebase service account JSON not found at: {service_account_path}"
            )

        service_account_json = json.loads(
            service_account_path.read_text(encoding="utf-8")
        )

        project_id = service_account_json.get("project_id") or service_account_json.get(
            "projectId"
        )
        if not isinstance(project_id, str) or not project_id.strip():
            raise KeyError(
                "Firebase service account JSON is missing `project_id` (or `projectId`)."
            )

        database_id = os.environ.get("FIRESTORE_DATABASE_ID", "a1-0000000")

        credentials = service_account.Credentials.from_service_account_file(
            str(service_account_path)
        )
        _client = firestore.Client(
            project=project_id,
            credentials=credentials,
            database=database_id,
        )
    return _client


def normalize_room_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).lower()


def room_name_exists(name: str) -> bool:
    normalized = normalize_room_name(name)
    db = get_client()
    q = db.collection("rooms").where("name_normalized", "==", normalized).limit(1)
    return next(q.stream(), None) is not None


def create_room(name: str, user_id: str) -> str:
    if room_name_exists(name):
        raise ValueError("A room with this name already exists.")
    db = get_client()
    now = datetime.now(timezone.utc)
    ref = db.collection("rooms").document()
    ref.set(
        {
            "name": name.strip(),
            "name_normalized": normalize_room_name(name),
            "created_by": user_id,
            "created_at": now,
        }
    )
    return ref.id


def list_rooms() -> list[dict[str, Any]]:
    db = get_client()
    rooms: list[dict[str, Any]] = []
    for doc in db.collection("rooms").order_by("name").stream():
        data = doc.to_dict() or {}
        rooms.append(
            {
                "id": doc.id,
                "name": data.get("name", ""),
                "created_by": data.get("created_by", ""),
            }
        )
    return rooms


def get_or_create_day(room_id: str, day: date) -> firestore.DocumentReference:
    db = get_client()
    day_id = day.isoformat()
    day_ref = db.collection("rooms").document(room_id).collection("days").document(day_id)
    snap = day_ref.get()
    if not snap.exists:
        day_ref.set(
            {
                "date": day_id,
                "room_id": room_id,
                "created_at": datetime.now(timezone.utc),
            }
        )
    return day_ref


def create_booking(
    room_id: str,
    day: date,
    *,
    start_time: str,
    end_time: str,
    user_id: str,
) -> str:
    day_ref = get_or_create_day(room_id, day)
    booking_ref = day_ref.collection("bookings").document()
    now = datetime.now(timezone.utc)
    booking_ref.set(
        {
            "start_time": start_time,
            "end_time": end_time,
            "created_by": user_id,
            "created_at": now,
            "room_id": room_id,
            "day": day.isoformat(),
        }
    )
    return booking_ref.id
