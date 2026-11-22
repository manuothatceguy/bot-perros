from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import time
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import jwt


logger = logging.getLogger(__name__)

GCAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GCAL_API_BASE = "https://www.googleapis.com/calendar/v3"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleCalendarClient:
    """Lightweight async Google Calendar v3 client using a Service Account JSON key."""

    def __init__(
        self,
        calendar_id: str,
        service_account_file: str,
        timeout: float = 15.0,
    ) -> None:
        self.calendar_id = (calendar_id or "").strip()
        self.service_account_file = (service_account_file or "").strip()
        self._client = httpx.AsyncClient(timeout=timeout)
        self._creds: dict[str, Any] | None = None
        self._access_token: str | None = None
        self._token_exp: float = 0.0
        self._load_creds()

    def _load_creds(self) -> None:
        if not self.calendar_id or not self.service_account_file:
            self._creds = None
            return
        path = Path(self.service_account_file)
        if not path.is_file():
            logger.warning("Google service account file not found: %s", path)
            self._creds = None
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("client_email") and data.get("private_key"):
                self._creds = data
            else:
                logger.warning("Invalid Google service account JSON in %s", path)
                self._creds = None
        except Exception as exc:
            logger.warning("Failed reading Google service account JSON: %s", exc)
            self._creds = None

    def is_configured(self) -> bool:
        return bool(self.calendar_id and self._creds is not None)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_access_token(self) -> str:
        if not self._creds:
            raise RuntimeError("Google Calendar no está configurado.")
        now = time.time()
        if self._access_token and now < (self._token_exp - 60):
            return self._access_token

        token_uri = str(self._creds.get("token_uri") or DEFAULT_TOKEN_URI)
        iat = int(now)
        claims = {
            "iss": self._creds["client_email"],
            "scope": GCAL_SCOPE,
            "aud": token_uri,
            "iat": iat,
            "exp": iat + 3600,
        }
        assertion = jwt.encode(claims, self._creds["private_key"], algorithm="RS256")
        resp = await self._client.post(
            token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        body = resp.json()
        self._access_token = str(body["access_token"])
        self._token_exp = now + float(body.get("expires_in", 3600))
        return self._access_token

    def _build_event_payload(self, appt: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        start_dt = datetime.fromtimestamp(int(appt["scheduled_at"]), tz=tz)
        end_dt = start_dt + timedelta(hours=1)
        desc_lines = [
            f"Perro: {appt['dog_name']}",
            f"Control: {appt['checkup_type']}",
        ]
        if appt.get("notes"):
            desc_lines.append(f"Notas: {appt['notes']}")
        payload: dict[str, Any] = {
            "summary": f"🐾 Vet {appt['dog_name']}: {appt['checkup_type']}",
            "description": "\n".join(desc_lines),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": str(tz)},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": str(tz)},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 1440},
                    {"method": "popup", "minutes": 120},
                ],
            },
        }
        if appt.get("location"):
            payload["location"] = str(appt["location"])
        return payload

    async def create_event(self, appt: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
        token = await self._get_access_token()
        cal_id = quote(self.calendar_id, safe="")
        resp = await self._client.post(
            f"{GCAL_API_BASE}/calendars/{cal_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            json=self._build_event_payload(appt, tz),
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "ok": True,
            "event_id": str(data.get("id") or ""),
            "html_link": str(data.get("htmlLink") or ""),
        }

    async def update_event(
        self, event_id: str, appt: dict[str, Any], tz: ZoneInfo
    ) -> dict[str, Any]:
        token = await self._get_access_token()
        cal_id = quote(self.calendar_id, safe="")
        ev_id = quote(event_id, safe="")
        resp = await self._client.patch(
            f"{GCAL_API_BASE}/calendars/{cal_id}/events/{ev_id}",
            headers={"Authorization": f"Bearer {token}"},
            json=self._build_event_payload(appt, tz),
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "ok": True,
            "event_id": str(data.get("id") or event_id),
            "html_link": str(data.get("htmlLink") or ""),
        }

    async def delete_event(self, event_id: str) -> bool:
        token = await self._get_access_token()
        cal_id = quote(self.calendar_id, safe="")
        ev_id = quote(event_id, safe="")
        resp = await self._client.delete(
            f"{GCAL_API_BASE}/calendars/{cal_id}/events/{ev_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code in (200, 204, 404, 410):
            return True
        resp.raise_for_status()
        return True
