from __future__ import annotations

import base64
from collections import OrderedDict
from datetime import datetime, timedelta
import logging
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.logic import build_ics_content, extract_wa_msg_key, format_appointment_card


logger = logging.getLogger(__name__)

BOT_WEBHOOK_EVENTS = [
    "message.any",
    "message.reaction",
    "poll.vote",
    "poll.vote.failed",
]


def normalize_jid(jid: str | None) -> str:
    """Normalize WhatsApp JID formatting."""
    if not jid:
        return ""
    value = str(jid).strip()
    if value.endswith("@s.whatsapp.net"):
        return value[: -len("@s.whatsapp.net")] + "@c.us"
    return value


class WahaClient:
    """Async HTTP client for WAHA REST API with sent-message tracking."""

    def __init__(
        self,
        base_url: str,
        session: str,
        api_key: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.api_key = api_key
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-Api-Key"] = api_key
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )
        self.sent_ids: OrderedDict[str, None] = OrderedDict()

    async def close(self) -> None:
        await self._client.aclose()

    def remember_sent_response(self, data: Any) -> str | None:
        """Track all message ID representations from a WAHA send response."""
        if not isinstance(data, dict):
            return None
        candidates: list[str] = []
        raw_id = data.get("id")
        if isinstance(raw_id, str) and raw_id:
            candidates.append(raw_id)
        elif isinstance(raw_id, dict) and raw_id.get("_serialized"):
            candidates.append(str(raw_id["_serialized"]))

        top_key = data.get("key")
        if isinstance(top_key, dict) and top_key.get("id"):
            candidates.append(str(top_key["id"]))

        _data = data.get("_data") or {}
        if isinstance(_data, dict):
            key = _data.get("key") or {}
            if isinstance(key, dict) and key.get("id"):
                candidates.append(str(key["id"]))
            inner_id = _data.get("id")
            if isinstance(inner_id, dict) and inner_id.get("_serialized"):
                candidates.append(str(inner_id["_serialized"]))

        for cid in list(candidates):
            canon = extract_wa_msg_key(cid)
            if canon:
                candidates.append(canon)

        primary: str | None = candidates[0] if candidates else None
        for cid in candidates:
            if not cid:
                continue
            self.sent_ids[cid] = None
            self.sent_ids.move_to_end(cid)
        while len(self.sent_ids) > 2000:
            self.sent_ids.popitem(last=False)
        return primary

    def is_sent_id(self, msg_id: str | None) -> bool:
        if not msg_id:
            return False
        if msg_id in self.sent_ids:
            return True
        canon = extract_wa_msg_key(msg_id)
        if canon and canon in self.sent_ids:
            return True
        return False

    async def get_session_info(self) -> dict[str, Any]:
        resp = await self._client.get(f"/api/sessions/{self.session}")
        resp.raise_for_status()
        return resp.json()

    async def configure_session_webhook(self, webhook_url: str) -> dict[str, Any]:
        """Idempotently ensure bot-perros webhook and noweb.store.enabled=true on the WAHA session."""
        info = await self.get_session_info()
        current_cfg = dict(info.get("config") or {})
        existing_hooks = list(current_cfg.get("webhooks") or [])

        preserved_hooks: list[dict[str, Any]] = []
        found_matching = False
        for hook in existing_hooks:
            if not isinstance(hook, dict):
                continue
            if hook.get("url") == webhook_url:
                if (
                    sorted(hook.get("events") or []) == sorted(BOT_WEBHOOK_EVENTS)
                    and not hook.get("hmac")
                ):
                    found_matching = True
                continue
            preserved_hooks.append(hook)

        desired_hook = {
            "url": webhook_url,
            "events": list(BOT_WEBHOOK_EVENTS),
        }
        desired_hooks = preserved_hooks + [desired_hook]

        current_noweb = dict(current_cfg.get("noweb") or {})
        current_store = dict(current_noweb.get("store") or {})
        store_already_ok = current_store.get("enabled") is True

        if found_matching and len(existing_hooks) == len(desired_hooks) and store_already_ok:
            return {"updated": False, "session": info}

        new_noweb = {
            **current_noweb,
            "markOnline": current_noweb.get("markOnline", True),
            "store": {
                "enabled": True,
                "fullSync": bool(current_store.get("fullSync", False)),
            },
        }
        new_cfg = {
            **current_cfg,
            "webhooks": desired_hooks,
            "noweb": new_noweb,
        }
        resp = await self._client.put(
            f"/api/sessions/{self.session}",
            json={"config": new_cfg},
        )
        resp.raise_for_status()
        return {"updated": True, "session": resp.json()}

    async def list_groups(self, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh:
            try:
                await self._client.post(f"/api/{self.session}/groups/refresh")
            except Exception as exc:
                logger.warning("WAHA groups refresh failed: %s", exc)
        resp = await self._client.get(f"/api/{self.session}/groups")
        resp.raise_for_status()
        raw = resp.json()
        items = list(raw.values()) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        groups: list[dict[str, Any]] = []
        for g in items:
            if not isinstance(g, dict):
                continue
            gid = g.get("id") or ""
            subject = g.get("subject") or g.get("name") or ""
            if gid:
                groups.append({"id": gid, "subject": subject})
        groups.sort(key=lambda x: (x["subject"] or x["id"]).lower())
        return groups

    async def get_group(self, group_id: str) -> dict[str, Any] | None:
        try:
            resp = await self._client.get(f"/api/{self.session}/groups/{quote(group_id, safe='')}")
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.debug("WAHA get_group(%s) failed: %s", group_id, exc)
        return None

    async def resolve_lid(self, lid: str) -> str | None:
        if not lid or not lid.endswith("@lid"):
            return normalize_jid(lid) if lid else None
        try:
            resp = await self._client.get(f"/api/{self.session}/lids/{quote(lid, safe='')}")
            if resp.status_code == 200:
                data = resp.json() or {}
                pn = data.get("pn") or data.get("phoneNumber")
                if pn:
                    norm = normalize_jid(pn)
                    return norm if "@" in norm else f"{norm}@c.us"
        except Exception as exc:
            logger.debug("WAHA resolve_lid(%s) failed: %s", lid, exc)
        return None

    async def get_contact_name(self, contact_id: str) -> str | None:
        if not contact_id:
            return None
        try:
            resp = await self._client.get(
                "/api/contacts",
                params={"session": self.session, "contactId": contact_id},
            )
            if resp.status_code == 200:
                data = resp.json() or {}
                name = data.get("name") or data.get("pushname") or data.get("shortName")
                if name and isinstance(name, str):
                    return name.strip() or None
        except Exception as exc:
            logger.debug("WAHA get_contact_name(%s) failed: %s", contact_id, exc)
        return None

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> str | None:
        payload: dict[str, Any] = {
            "session": self.session,
            "chatId": chat_id,
            "text": text,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        resp = await self._client.post("/api/sendText", json=payload)
        resp.raise_for_status()
        return self.remember_sent_response(resp.json())

    async def send_poll(
        self,
        chat_id: str,
        name: str,
        options: list[str],
        multiple_answers: bool = False,
    ) -> str | None:
        clean_options = [opt[:100] for opt in options[:12]]
        if len(clean_options) < 2:
            raise ValueError("Poll requires at least 2 options")
        payload = {
            "session": self.session,
            "chatId": chat_id,
            "poll": {
                "name": name[:255],
                "options": clean_options,
                "multipleAnswers": bool(multiple_answers),
            },
        }
        resp = await self._client.post("/api/sendPoll", json=payload)
        resp.raise_for_status()
        return self.remember_sent_response(resp.json())

    async def send_image_base64(
        self,
        chat_id: str,
        filename: str,
        mimetype: str,
        data_bytes: bytes,
        caption: str = "",
    ) -> str | None:
        b64 = base64.b64encode(data_bytes).decode("ascii")
        payload: dict[str, Any] = {
            "session": self.session,
            "chatId": chat_id,
            "file": {
                "mimetype": mimetype,
                "filename": filename,
                "data": b64,
            },
        }
        if caption:
            payload["caption"] = caption
        resp = await self._client.post("/api/sendImage", json=payload)
        resp.raise_for_status()
        return self.remember_sent_response(resp.json())

    async def send_file_base64(
        self,
        chat_id: str,
        filename: str,
        mimetype: str,
        data_bytes: bytes,
        caption: str = "",
    ) -> str | None:
        b64 = base64.b64encode(data_bytes).decode("ascii")
        payload: dict[str, Any] = {
            "session": self.session,
            "chatId": chat_id,
            "file": {
                "mimetype": mimetype,
                "filename": filename,
                "data": b64,
            },
        }
        if caption:
            payload["caption"] = caption
        resp = await self._client.post("/api/sendFile", json=payload)
        resp.raise_for_status()
        return self.remember_sent_response(resp.json())

    async def publish_appointment_event(
        self, chat_id: str, appt: dict[str, Any], tz: ZoneInfo
    ) -> dict[str, str]:
        """Try native WhatsApp Event (/events); fall back to .ics file attachment or formatted text."""
        start_ts = int(appt["scheduled_at"])
        dt = datetime.fromtimestamp(start_ts, tz=tz)
        end_ts = int((dt + timedelta(hours=1)).timestamp())

        desc_lines = [
            f"🐾 Perro: {appt['dog_name']}",
            f"🩺 Control: {appt['checkup_type']}",
        ]
        if appt.get("notes"):
            desc_lines.append(f"📝 Notas: {appt['notes']}")

        event_obj: dict[str, Any] = {
            "name": f"Vet {appt['dog_name']}: {appt['checkup_type']}",
            "description": "\n".join(desc_lines),
            "startTime": start_ts,
            "endTime": end_ts,
        }
        if appt.get("location"):
            event_obj["location"] = {"name": appt["location"]}

        try:
            resp = await self._client.post(
                f"/api/{self.session}/events",
                json={"chatId": chat_id, "event": event_obj},
            )
            if 200 <= resp.status_code < 300:
                msg_id = self.remember_sent_response(resp.json())
                return {"mode": "event", "message_id": msg_id or ""}
            logger.info(
                "WAHA /events returned %s (%s); falling back to .ics",
                resp.status_code,
                resp.text[:200],
            )
        except Exception as exc:
            logger.info("WAHA /events failed (%s); falling back to .ics", exc)

        caption = format_appointment_card(appt, tz, prefix="📅 *Nuevo Turno Veterinario*")
        ics_bytes = build_ics_content(appt, tz)
        safe_dog = "".join(c for c in appt["dog_name"].lower() if c.isalnum()) or "perro"
        filename = f"turno_{safe_dog}_{dt.strftime('%Y%m%d_%H%M')}.ics"

        try:
            msg_id = await self.send_file_base64(
                chat_id=chat_id,
                filename=filename,
                mimetype="text/calendar",
                data_bytes=ics_bytes,
                caption=caption,
            )
            return {"mode": "ics", "message_id": msg_id or ""}
        except Exception as exc:
            logger.warning("WAHA sendFile .ics failed (%s); falling back to text", exc)
            msg_id = await self.send_text(chat_id, caption)
            return {"mode": "text", "message_id": msg_id or ""}
