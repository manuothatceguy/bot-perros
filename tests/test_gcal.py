from __future__ import annotations

import json
import sqlite3
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.gcal import GoogleCalendarClient
from app.main import create_app
from tests.conftest import FakeWahaClient


def _write_test_service_account(path) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    sa = {
        "type": "service_account",
        "client_email": "bot-test@bot-perros.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    path.write_text(json.dumps(sa), encoding="utf-8")
    return str(path)


@pytest.mark.asyncio
async def test_google_calendar_client_jwt_create_update_delete(tmp_path):
    sa_file = _write_test_service_account(tmp_path / "sa.json")
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            calls.append(("POST", "/token", None))
            return httpx.Response(200, json={"access_token": "tok_123", "expires_in": 3600})
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "POST":
            assert request.headers["Authorization"] == "Bearer tok_123"
            return httpx.Response(200, json={"id": "gcal_ev_1", "htmlLink": "https://calendar.google.com/ev1"})
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": "gcal_ev_1", "htmlLink": "https://calendar.google.com/ev1"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    gcal = GoogleCalendarClient("cal123@group.calendar.google.com", sa_file)
    await gcal._client.aclose()
    gcal._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tz = ZoneInfo("America/Argentina/Buenos_Aires")
    appt = {
        "id": 1,
        "dog_name": "Pancho",
        "checkup_type": "Cardiología",
        "location": "Vet Central",
        "scheduled_at": 1795600000,
        "notes": "Llevar estudios",
    }

    try:
        assert gcal.is_configured() is True
        created = await gcal.create_event(appt, tz)
        assert created["ok"] is True
        assert created["event_id"] == "gcal_ev_1"

        updated = await gcal.update_event("gcal_ev_1", appt, tz)
        assert updated["ok"] is True

        deleted = await gcal.delete_event("gcal_ev_1")
        assert deleted is True

        # Token fetched only ONCE (cached across create/update/delete)
        token_calls = [c for c in calls if c[1] == "/token"]
        assert len(token_calls) == 1
    finally:
        await gcal.close()


def test_appointment_publishes_to_whatsapp_and_gcal_and_deletes(
    tmp_path, settings: Settings, db: Database, fake_waha: FakeWahaClient
):
    fake_waha.events_supported = True
    sa_file = _write_test_service_account(tmp_path / "sa2.json")
    gcal_ops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok_abc", "expires_in": 3600})
        gcal_ops.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "gcal_99", "htmlLink": "https://cal/99"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"id": "gcal_99"})

    gcal = GoogleCalendarClient("cal123@group.calendar.google.com", sa_file)
    gcal._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(
        settings=settings,
        db=db,
        waha=fake_waha,  # type: ignore[arg-type]
        gcal=gcal,
        enable_scheduler=False,
    )

    with TestClient(app) as client:
        assert client.get("/health").json()["gcal_configured"] is True
        r = client.post(
            "/api/appointments",
            json={
                "dog_name": "Pancho",
                "checkup_type": "Control anual",
                "location": "Vet",
                "scheduled_at": 1795600000,
                "notes": "Ayunas",
                "publish_to_whatsapp": True,
                "publish_to_gcal": True,
            },
        )
        assert r.status_code == 201
        data = r.json()
        assert data["publish_result"]["mode"] == "event"
        assert data["publish_result"]["gcal"]["ok"] is True
        assert data["appointment"]["google_calendar_event_id"] == "gcal_99"

        appt_id = data["appointment"]["id"]
        assert client.delete(f"/api/appointments/{appt_id}").status_code == 200
        assert gcal_ops == ["POST", "DELETE"]


def test_db_migrates_existing_appointments_table_without_gcal_column(tmp_path):
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dog_id INTEGER,
            dog_name TEXT NOT NULL,
            checkup_type TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            scheduled_at INTEGER NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            reminded_24h INTEGER NOT NULL DEFAULT 0,
            reminded_2h INTEGER NOT NULL DEFAULT 0,
            whatsapp_event_sent INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    migrated = Database(str(db_file))
    try:
        cols = {
            r["name"]
            for r in migrated.conn.execute("PRAGMA table_info(appointments)").fetchall()
        }
        assert "google_calendar_event_id" in cols
    finally:
        migrated.close()
