from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Callable, TypeVar

import firebase_admin
from firebase_admin import auth as firebase_auth
from flask import redirect, session, url_for

F = TypeVar("F", bound=Callable[..., Any])


def init_firebase() -> None:
    try:
        firebase_admin.get_app()
        return
    except ValueError:
        pass
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

    credentials_object = firebase_admin.credentials.Certificate(
        str(service_account_path)
    )
    firebase_admin.initialize_app(credentials_object, options={"projectId": project_id})


def verify_id_token(id_token: str) -> dict[str, Any]:
    return firebase_auth.verify_id_token(id_token)


def login_required(view: F) -> F:
    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not session.get("uid"):
            return redirect(url_for("main.login"))
        return view(*args, **kwargs)

    return wrapped


def current_user_id() -> str | None:
    return session.get("uid")
