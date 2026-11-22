from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.bot import DogBot
from app.config import Settings
from app.db import Database
from app.logic import build_ics_content, format_appointment_card
from app.waha import normalize_jid


class FakeWahaClient:
    """In-memory test double for WahaClient matching its public interface."""

    def __init__(self) -> None:
        self.sent_ids: OrderedDict[str, None] = OrderedDict()
        self.sent_texts: list[dict[str, Any]] = []
        self.sent_polls: list[dict[str, Any]] = []
        self.sent_files: list[dict[str, Any]] = []
        self.sent_images: list[dict[str, Any]] = []
        self.event_attempts: list[dict[str, Any]] = []
        self.events_supported: bool = False
        self.files_supported: bool = True
        self.lids_map: dict[str, str] = {}
        self.contacts_map: dict[str, str] = {}
        self.groups: list[dict[str, Any]] = [
            {"id": "120363000000001@g.us", "subject": "Perros"}
        ]
        self.session_info: dict[str, Any] = {
            "name": "session_test",
            "status": "WORKING",
            "me": {"id": "5491100000000@c.us", "pushName": "Bot"},
            "config": {"webhooks": [], "noweb": {"markOnline": True, "store": {"enabled": False}}},
        }
        self._seq = 0

    def _next_id(self, chat_id: str) -> str:
        self._seq += 1
        msg_id = f"true_{chat_id}_BOTMSG{self._seq}"
        self.sent_ids[msg_id] = None
        self.sent_ids[f"BOTMSG{self._seq}"] = None
        return msg_id

    def is_sent_id(self, msg_id: str | None) -> bool:
        if not msg_id:
            return False
        if msg_id in self.sent_ids:
            return True
        if "_" in msg_id and msg_id.rsplit("_", 1)[-1] in self.sent_ids:
            return True
        return False

    async def close(self) -> None:
        return None

    async def get_session_info(self) -> dict[str, Any]:
        return self.session_info

    async def configure_session_webhook(self, webhook_url: str) -> dict[str, Any]:
        self.session_info["config"]["webhooks"] = [
            {"url": webhook_url, "events": ["message.any", "message.reaction", "poll.vote", "poll.vote.failed"]}
        ]
        self.session_info["config"]["noweb"]["store"]["enabled"] = True
        return {"updated": True, "session": self.session_info}

    async def list_groups(self, refresh: bool = False) -> list[dict[str, Any]]:
        return list(self.groups)

    async def get_group(self, group_id: str) -> dict[str, Any] | None:
        for g in self.groups:
            if g["id"] == group_id:
                return g
        return None

    async def resolve_lid(self, lid: str) -> str | None:
        return self.lids_map.get(lid)

    async def get_contact_name(self, contact_id: str) -> str | None:
        return self.contacts_map.get(contact_id)

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> str | None:
        msg_id = self._next_id(chat_id)
        self.sent_texts.append(
            {"id": msg_id, "chat_id": chat_id, "text": text, "reply_to": reply_to}
        )
        return msg_id

    async def send_poll(
        self,
        chat_id: str,
        name: str,
        options: list[str],
        multiple_answers: bool = False,
    ) -> str | None:
        msg_id = self._next_id(chat_id)
        self.sent_polls.append(
            {
                "id": msg_id,
                "chat_id": chat_id,
                "name": name,
                "options": list(options),
                "multiple_answers": multiple_answers,
            }
        )
        return msg_id

    async def send_image_base64(
        self,
        chat_id: str,
        filename: str,
        mimetype: str,
        data_bytes: bytes,
        caption: str = "",
    ) -> str | None:
        msg_id = self._next_id(chat_id)
        self.sent_images.append(
            {
                "id": msg_id,
                "chat_id": chat_id,
                "filename": filename,
                "mimetype": mimetype,
                "data": data_bytes,
                "caption": caption,
            }
        )
        return msg_id

    async def send_file_base64(
        self,
        chat_id: str,
        filename: str,
        mimetype: str,
        data_bytes: bytes,
        caption: str = "",
    ) -> str | None:
        if not self.files_supported:
            raise RuntimeError("sendFile disabled in test")
        msg_id = self._next_id(chat_id)
        self.sent_files.append(
            {
                "id": msg_id,
                "chat_id": chat_id,
                "filename": filename,
                "mimetype": mimetype,
                "data": data_bytes,
                "caption": caption,
            }
        )
        return msg_id

    async def publish_appointment_event(
        self, chat_id: str, appt: dict[str, Any], tz: ZoneInfo
    ) -> dict[str, str]:
        self.event_attempts.append({"chat_id": chat_id, "appt": dict(appt)})
        if self.events_supported:
            msg_id = self._next_id(chat_id)
            return {"mode": "event", "message_id": msg_id}
        caption = format_appointment_card(appt, tz, prefix="📅 *Nuevo Turno Veterinario*")
        if self.files_supported:
            ics = build_ics_content(appt, tz)
            dt = datetime.fromtimestamp(int(appt["scheduled_at"]), tz=tz)
            msg_id = await self.send_file_base64(
                chat_id=chat_id,
                filename=f"turno_{appt['dog_name'].lower()}_{dt.strftime('%Y%m%d_%H%M')}.ics",
                mimetype="text/calendar",
                data_bytes=ics,
                caption=caption,
            )
            return {"mode": "ics", "message_id": msg_id or ""}
        msg_id = await self.send_text(chat_id, caption)
        return {"mode": "text", "message_id": msg_id or ""}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        tz="America/Argentina/Buenos_Aires",
        waha_url="http://waha:3000",
        waha_session="session_test",
        whatsapp_group_id="120363000000001@g.us",
        whatsapp_group_name="Perros",
        db_path=str(tmp_path / "test_bot.db"),
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.db_path, settings=settings)
    yield database
    database.close()


@pytest.fixture
def fake_waha() -> FakeWahaClient:
    return FakeWahaClient()


@pytest.fixture
def bot(db: Database, fake_waha: FakeWahaClient, settings: Settings) -> DogBot:
    return DogBot(db=db, waha=fake_waha, settings=settings)  # type: ignore[arg-type]
