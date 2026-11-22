from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import logging
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import Database
from app.gcal import GoogleCalendarClient
from app.logic import (
    MEAL_LABELS,
    TIMING_LABELS,
    calculate_stock_status,
    format_appointment_card,
    format_appointments_list,
    format_dm_checklist,
    format_qty,
    format_step2_only,
    format_stock_list,
    format_unit,
    get_due_meds,
    get_max_fasting_offset,
    normalize_text,
    parse_appointment_command,
    parse_date,
    parse_qty_text,
)
from app.photos import build_reference_image, get_photos_dir
from app.waha import WahaClient, normalize_jid


logger = logging.getLogger(__name__)

GROUP_CLAIM_EXACT = {"yo", "doy", "yo doy", "yo les doy", "doy almuerzo", "doy cena"}
GROUP_CONFIRM_EXACT = {"almuerzo listo", "cena lista", "ya les di", "ya comieron"}
DM_FASTING_EXACT = {"1", "ayuno", "ayuno listo"}
DM_CONFIRM_EXACT = {"2", "listo", "ok", "termine", "almuerzo listo", "cena lista"}
DM_RELEASE_EXACT = {"cancelar", "no puedo", "liberar"}

GROUP_CLAIM_EMOJIS = {"✋", "🖐️", "🖐", "🙋", "🙋‍♂️", "🙋‍♀️", "👍"}
GROUP_CONFIRM_EMOJIS = {"✅", "☑️", "✔️"}
DM_FASTING_EMOJIS = {"👍", "⏱️", "⏱", "1️⃣"}
DM_CONFIRM_EMOJIS = {"✅", "☑️", "✔️", "2️⃣"}
DM_RELEASE_EMOJIS = {"❌", "✖️"}


def _join_names(names: list[str]) -> str:
    if not names:
        return "los perros"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} y {names[1]}"
    return ", ".join(names[:-1]) + f" y {names[-1]}"


class DogBot:
    """Core bot orchestration for WhatsApp interactions, meals, stock, and appointments."""

    def __init__(
        self,
        db: Database,
        waha: WahaClient,
        settings: Settings,
        gcal: GoogleCalendarClient | None = None,
    ) -> None:
        self.db = db
        self.waha = waha
        self.settings = settings
        self.gcal = gcal
        self.tz: ZoneInfo = settings.get_tz()
        self.photos_dir = get_photos_dir(db.db_path)
        self._me_id: str | None = None
        self._me_lid: str | None = None

    def get_group_id(self) -> str:
        return self.db.get_setting("whatsapp_group_id", "").strip()

    def get_group_name(self) -> str:
        return self.db.get_setting("whatsapp_group_name", self.settings.whatsapp_group_name).strip()

    def _active_dog_names_str(self) -> str:
        dogs = self.db.list_dogs(active_only=True)
        return _join_names([d["name"] for d in dogs])

    def _meal_time_str(self, meal: str) -> str:
        if meal == "lunch":
            return self.db.get_setting("lunch_time", self.settings.lunch_time)
        return self.db.get_setting("dinner_time", self.settings.dinner_time)

    def _infer_active_meal(self, now: datetime | None = None) -> str:
        current = now.astimezone(self.tz) if now else datetime.now(tz=self.tz)
        date_str = current.date().isoformat()
        logs = self.db.get_dose_logs_for_date(date_str)
        lunch_log = logs.get("lunch")
        dinner_log = logs.get("dinner")
        if lunch_log and not lunch_log.get("given_at") and (
            lunch_log.get("reminder_sent_at") or lunch_log.get("claimed_at")
        ):
            return "lunch"
        if dinner_log and not dinner_log.get("given_at") and (
            dinner_log.get("reminder_sent_at") or dinner_log.get("claimed_at")
        ):
            return "dinner"
        if current.hour < 16:
            return "lunch"
        return "dinner"

    async def _get_me_jid(self) -> str | None:
        if self._me_id:
            return self._me_id
        try:
            info = await self.waha.get_session_info()
            me = info.get("me") or {}
            me_id = normalize_jid(me.get("id") or "")
            me_lid = normalize_jid(me.get("lid") or "")
            if me_lid:
                self._me_lid = me_lid
            if me_id:
                self._me_id = me_id
                return me_id
        except Exception as exc:
            logger.debug("Could not fetch session me info: %s", exc)
        return None

    async def sync_group_participants(self, group_id: str | None = None) -> int:
        """Populate family_members from WhatsApp group participants (skipping the bot itself)."""
        gid = group_id or self.get_group_id()
        if not gid:
            return 0
        group = await self.waha.get_group(gid)
        if not group:
            return 0
        participants = group.get("participants") or []
        if not participants:
            return 0

        me_jid = await self._get_me_jid()
        me_lid = self._me_lid
        synced = 0
        for p in participants:
            if not isinstance(p, dict):
                continue
            p_id = normalize_jid(p.get("id") or "")
            p_pn = normalize_jid(p.get("phoneNumber") or "")
            if p_id in (me_jid, me_lid) or (p_pn and p_pn == me_jid):
                continue
            cus = p_pn if p_pn.endswith("@c.us") else (p_id if p_id.endswith("@c.us") else "")
            lid = p_id if p_id.endswith("@lid") else None
            jid = cus or lid
            if not jid:
                continue
            existing = (
                self.db.get_family_member_by_jid_or_lid(jid)
                or (self.db.get_family_member_by_jid_or_lid(lid) if lid else None)
            )
            display_name = None
            if existing and existing.get("custom_name"):
                display_name = existing["display_name"]
            else:
                display_name = await self.waha.get_contact_name(jid)
                if not display_name and existing:
                    display_name = existing["display_name"]
            self.db.upsert_family_member(
                jid=jid,
                lid=lid,
                display_name=display_name,
                custom_name=False,
            )
            synced += 1
        return synced

    async def resolve_actor(
        self,
        raw_jid: str | None,
        alt_jid: str | None = None,
        push_name: str | None = None,
    ) -> tuple[str, str]:
        """Resolve WhatsApp JID (preferably @c.us) and human display name."""
        norm_raw = normalize_jid(raw_jid)
        norm_alt = normalize_jid(alt_jid)
        if norm_raw == "me":
            me_jid = await self._get_me_jid()
            if me_jid:
                norm_raw = me_jid

        lid = None
        if norm_raw.endswith("@lid"):
            lid = norm_raw
        elif norm_alt.endswith("@lid"):
            lid = norm_alt

        dm_jid = ""
        if norm_alt.endswith("@c.us"):
            dm_jid = norm_alt
        elif norm_raw.endswith("@c.us"):
            dm_jid = norm_raw

        member = (
            self.db.get_family_member_by_jid_or_lid(dm_jid)
            or self.db.get_family_member_by_jid_or_lid(norm_raw)
            or (self.db.get_family_member_by_jid_or_lid(lid) if lid else None)
        )
        if member and not dm_jid and member["jid"].endswith("@c.us"):
            dm_jid = member["jid"]

        if not dm_jid and lid:
            await self.sync_group_participants()
            member = self.db.get_family_member_by_jid_or_lid(lid) or member
            if member and member["jid"].endswith("@c.us"):
                dm_jid = member["jid"]

        if not dm_jid and lid:
            resolved = await self.waha.resolve_lid(lid)
            if resolved:
                dm_jid = resolved

        if not dm_jid:
            dm_jid = norm_raw or norm_alt

        clean_push = (push_name or "").strip()
        if member and member.get("custom_name"):
            display_name = member["display_name"]
        elif clean_push:
            display_name = clean_push
        elif member and member.get("display_name") and member["display_name"] != member["jid"].split("@", 1)[0]:
            display_name = member["display_name"]
        else:
            contact_name = await self.waha.get_contact_name(dm_jid)
            display_name = contact_name or (dm_jid.split("@", 1)[0] if dm_jid else "Familia")

        if dm_jid and not dm_jid.endswith("@g.us") and dm_jid != "me":
            self.db.upsert_family_member(
                jid=dm_jid,
                lid=lid,
                display_name=display_name,
                custom_name=False,
            )

        return dm_jid, display_name

    # ---- Webhook Handling ----

    def _build_dedup_key(self, body: dict[str, Any]) -> str:
        event = str(body.get("event") or "")
        payload = body.get("payload") or {}
        if event in ("message", "message.any"):
            msg_id = payload.get("id")
            if msg_id:
                return f"msg:{msg_id}"
        elif event == "message.reaction":
            reaction = payload.get("reaction") or {}
            return (
                f"react:{payload.get('id') or ''}:"
                f"{reaction.get('messageId') or ''}:"
                f"{reaction.get('text') or ''}"
            )
        elif event == "poll.vote":
            vote = payload.get("vote") or {}
            poll = payload.get("poll") or {}
            opts = ",".join(sorted(str(o) for o in (vote.get("selectedOptions") or [])))
            return (
                f"vote:{poll.get('id') or ''}:"
                f"{vote.get('from') or ''}:"
                f"{vote.get('timestamp') or ''}:"
                f"{opts}"
            )
        raw = json.dumps(body, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return f"evt:{event}:{digest}"

    async def handle_webhook(self, body: dict[str, Any]) -> dict[str, Any]:
        """Process an incoming WAHA webhook payload."""
        event = str(body.get("event") or "")
        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            return {"status": "ignored", "reason": "invalid_payload"}

        # Ignore bot-authored message echoes BEFORE marking dedup
        if event in ("message", "message.any"):
            msg_id = str(payload.get("id") or "")
            raw_key_id = str(((payload.get("_data") or {}).get("key") or {}).get("id") or "")
            if self.waha.is_sent_id(msg_id) or self.waha.is_sent_id(raw_key_id):
                return {"status": "ignored", "reason": "bot_sent_echo"}

        dedup_key = self._build_dedup_key(body)
        if not self.db.mark_event_processed(dedup_key):
            return {"status": "ignored", "reason": "duplicate"}

        if event == "poll.vote":
            return await self._handle_poll_vote(payload)
        if event == "message.reaction":
            return await self._handle_reaction(payload)
        if event in ("message", "message.any"):
            return await self._handle_message(payload)
        return {"status": "ignored", "reason": f"unhandled_event:{event}"}

    async def _handle_poll_vote(self, payload: dict[str, Any]) -> dict[str, Any]:
        vote = payload.get("vote") or {}
        poll = payload.get("poll") or {}
        selected = [str(o) for o in (vote.get("selectedOptions") or [])]
        if not selected:
            return {"status": "ignored", "reason": "empty_vote"}

        poll_id = str(poll.get("id") or "")
        found = self.db.find_dose_log_by_poll_msg_id(poll_id)
        if not found:
            return {"status": "ignored", "reason": "unknown_poll"}

        log_row, kind = found
        date_str = log_row["date"]
        meal = log_row["meal"]

        if kind == "dm":
            voter_raw = normalize_jid(vote.get("from") or "")
            if voter_raw == "me" or vote.get("fromMe"):
                voter_raw = log_row.get("claimed_by_jid") or normalize_jid(vote.get("to") or "") or voter_raw
            actor_jid, actor_name = await self.resolve_actor(
                voter_raw,
                push_name=log_row.get("claimed_by") if voter_raw == log_row.get("claimed_by_jid") else None,
            )
            # Multi-select priority: confirm -> release -> fasting
            if any(opt.startswith("✅") for opt in selected):
                await self.confirm_meal(date_str, meal, actor_jid, actor_name, from_dm=True)
                return {"status": "ok", "action": "confirm_dm_poll"}
            if any(opt.startswith("❌") for opt in selected):
                await self.release_meal(date_str, meal, actor_jid, actor_name)
                return {"status": "ok", "action": "release_dm_poll"}
            if any(opt.startswith("⏱") for opt in selected):
                await self.mark_fasting_done(date_str, meal, actor_jid, actor_name)
                return {"status": "ok", "action": "fasting_dm_poll"}
            return {"status": "ignored", "reason": "unrecognized_dm_option"}

        # Group poll
        voter_raw = normalize_jid(vote.get("from") or "")
        actor_jid, actor_name = await self.resolve_actor(voter_raw)
        if any(opt.startswith("✅") for opt in selected):
            await self.confirm_meal(date_str, meal, actor_jid, actor_name, from_dm=False)
            return {"status": "ok", "action": "confirm_group_poll"}
        if any(opt.startswith("✋") for opt in selected):
            await self.claim_meal(
                date_str,
                meal,
                actor_jid,
                actor_name,
                reply_chat_id=self.get_group_id() or poll.get("to") or poll.get("from"),
            )
            return {"status": "ok", "action": "claim_group_poll"}
        return {"status": "ignored", "reason": "unrecognized_group_option"}

    async def _handle_reaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        reaction = payload.get("reaction") or {}
        emoji = str(reaction.get("text") or "").strip()
        target_msg_id = str(reaction.get("messageId") or "")
        if not emoji or not target_msg_id:
            return {"status": "ignored", "reason": "empty_reaction"}

        found = self.db.find_dose_log_by_poll_msg_id(target_msg_id)
        if not found:
            return {"status": "ignored", "reason": "unknown_target"}

        log_row, kind = found
        date_str = log_row["date"]
        meal = log_row["meal"]
        key = (payload.get("_data") or {}).get("key") or {}
        is_group = str(payload.get("from") or "").endswith("@g.us")
        raw_sender = (
            payload.get("participant") or key.get("participant") or payload.get("from")
            if is_group
            else payload.get("from")
        )
        alt_sender = key.get("participantAlt") if is_group else key.get("remoteJidAlt")
        push_name = payload.get("notifyName") or (payload.get("_data") or {}).get("pushName")
        actor_jid, actor_name = await self.resolve_actor(raw_sender, alt_sender, push_name)

        if kind == "group":
            if emoji in GROUP_CONFIRM_EMOJIS:
                await self.confirm_meal(date_str, meal, actor_jid, actor_name, from_dm=False)
                return {"status": "ok", "action": "confirm_group_reaction"}
            if emoji in GROUP_CLAIM_EMOJIS:
                await self.claim_meal(
                    date_str,
                    meal,
                    actor_jid,
                    actor_name,
                    reply_chat_id=normalize_jid(payload.get("from")),
                )
                return {"status": "ok", "action": "claim_group_reaction"}
            return {"status": "ignored", "reason": "unhandled_group_emoji"}

        # DM reaction
        if emoji in DM_CONFIRM_EMOJIS:
            await self.confirm_meal(date_str, meal, actor_jid, actor_name, from_dm=True)
            return {"status": "ok", "action": "confirm_dm_reaction"}
        if emoji in DM_FASTING_EMOJIS:
            await self.mark_fasting_done(date_str, meal, actor_jid, actor_name)
            return {"status": "ok", "action": "fasting_dm_reaction"}
        if emoji in DM_RELEASE_EMOJIS:
            await self.release_meal(date_str, meal, actor_jid, actor_name)
            return {"status": "ok", "action": "release_dm_reaction"}
        return {"status": "ignored", "reason": "unhandled_dm_emoji"}

    async def _maybe_autobind_group(self, group_jid: str) -> None:
        if self.get_group_id():
            return
        expected = normalize_text(self.get_group_name())
        if not expected:
            return
        info = await self.waha.get_group(group_jid)
        if info:
            subj = normalize_text(str(info.get("subject") or info.get("name") or ""))
            if subj == expected:
                self.db.set_setting("whatsapp_group_id", group_jid)
                logger.info("Auto-bound WhatsApp group '%s' -> %s", info.get("subject"), group_jid)
                return
        try:
            groups = await self.waha.list_groups(refresh=False)
            for g in groups:
                if g["id"] == group_jid and normalize_text(g["subject"]) == expected:
                    self.db.set_setting("whatsapp_group_id", group_jid)
                    logger.info("Auto-bound WhatsApp group '%s' -> %s", g["subject"], group_jid)
                    return
        except Exception as exc:
            logger.debug("Group list lookup during autobind failed: %s", exc)

    async def _handle_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        body_text = str(payload.get("body") or "").strip()
        if not body_text:
            return {"status": "ignored", "reason": "empty_body"}

        chat_id = normalize_jid(payload.get("from") or "")
        if not chat_id or chat_id == "status@broadcast":
            return {"status": "ignored", "reason": "ignored_chat"}

        is_group = chat_id.endswith("@g.us")
        key = (payload.get("_data") or {}).get("key") or {}
        raw_sender = (
            payload.get("participant") or payload.get("author") or key.get("participant") or chat_id
            if is_group
            else chat_id
        )
        alt_sender = key.get("participantAlt") if is_group else key.get("remoteJidAlt")
        push_name = payload.get("notifyName") or (payload.get("_data") or {}).get("pushName")
        actor_jid, actor_name = await self.resolve_actor(raw_sender, alt_sender, push_name)

        if is_group:
            await self._maybe_autobind_group(chat_id)

        configured_group = self.get_group_id()
        if is_group and configured_group and chat_id != configured_group:
            # Allow /grupo anywhere to rebind if needed; ignore other unrelated groups
            if not normalize_text(body_text).startswith("/grupo"):
                return {"status": "ignored", "reason": "other_group"}

        norm = normalize_text(body_text)
        now = datetime.now(tz=self.tz)
        today_str = now.date().isoformat()

        # 1. Explicit slash commands (work in group and DM)
        if body_text.startswith("/"):
            return await self._handle_slash_command(
                body_text=body_text,
                norm=norm,
                chat_id=chat_id,
                is_group=is_group,
                actor_jid=actor_jid,
                actor_name=actor_name,
                now=now,
            )

        # 2. Tight natural text fallbacks in configured family group
        if is_group:
            meal = self._infer_active_meal(now)
            log_row = self.db.get_dose_log(today_str, meal)
            has_active_unconfirmed = bool(
                log_row and not log_row.get("given_at") and (log_row.get("reminder_sent_at") or log_row.get("followup_sent_at"))
            )
            if has_active_unconfirmed:
                if norm in GROUP_CLAIM_EXACT:
                    await self.claim_meal(today_str, meal, actor_jid, actor_name, reply_chat_id=chat_id)
                    return {"status": "ok", "action": "claim_group_text"}
                if norm in GROUP_CONFIRM_EXACT:
                    await self.confirm_meal(today_str, meal, actor_jid, actor_name, from_dm=False)
                    return {"status": "ok", "action": "confirm_group_text"}
            return {"status": "ignored", "reason": "casual_group_chat"}

        # 3. Tight natural text fallbacks in DM (ONLY when sender holds an active claim today)
        sender_candidates = [actor_jid, normalize_jid(raw_sender), normalize_jid(alt_sender), chat_id]
        active_claim = self.db.find_active_claim_for_sender(today_str, sender_candidates)
        if active_claim:
            meal = active_claim["meal"]
            if norm in DM_FASTING_EXACT:
                await self.mark_fasting_done(today_str, meal, actor_jid, actor_name)
                return {"status": "ok", "action": "fasting_dm_text"}
            if norm in DM_CONFIRM_EXACT:
                await self.confirm_meal(today_str, meal, actor_jid, actor_name, from_dm=True)
                return {"status": "ok", "action": "confirm_dm_text"}
            if norm in DM_RELEASE_EXACT:
                await self.release_meal(today_str, meal, actor_jid, actor_name)
                return {"status": "ok", "action": "release_dm_text"}

        return {"status": "ignored", "reason": "unrecognized_dm_text"}

    async def _handle_slash_command(
        self,
        *,
        body_text: str,
        norm: str,
        chat_id: str,
        is_group: bool,
        actor_jid: str,
        actor_name: str,
        now: datetime,
    ) -> dict[str, Any]:
        cmd = norm.split(" ", 1)[0]
        today = now.date()
        today_str = today.isoformat()

        if cmd in ("/ayuda", "/help"):
            await self.waha.send_text(chat_id, self._help_text())
            return {"status": "ok", "action": "help"}

        if cmd == "/grupo":
            if not is_group:
                await self.waha.send_text(chat_id, "Este comando se manda desde el grupo de WhatsApp familiar.")
                return {"status": "ok", "action": "grupo_not_group"}
            self.db.set_setting("whatsapp_group_id", chat_id)
            await self.waha.send_text(
                chat_id,
                f"✅ ¡Listo! Este grupo quedó vinculado para gestionar a {self._active_dog_names_str()}.",
            )
            return {"status": "ok", "action": "grupo_bound"}

        if cmd == "/dar":
            meal = self._infer_active_meal(now)
            if "almuerzo" in norm:
                meal = "lunch"
            elif "cena" in norm:
                meal = "dinner"
            await self.claim_meal(today_str, meal, actor_jid, actor_name, reply_chat_id=chat_id)
            return {"status": "ok", "action": "dar_command"}

        if cmd == "/stock":
            meds = self.db.list_medications(active_only=False)
            logs = self.db.get_dose_logs_for_date(today_str)
            given_meals = {m for m, row in logs.items() if row.get("given_at")}
            await self.waha.send_text(chat_id, format_stock_list(meds, today, given_meals))
            return {"status": "ok", "action": "stock"}

        if cmd in ("/compre", "/set"):
            mode = "purchase" if cmd == "/compre" else "set"
            msg = self._apply_stock_command(body_text, mode=mode, actor_name=actor_name, today=today, today_str=today_str)
            await self.waha.send_text(chat_id, msg)
            return {"status": "ok", "action": mode}

        if cmd == "/turnos":
            appts = self.db.list_appointments(upcoming_only=True, from_ts=int(now.timestamp()) - 3600)
            await self.waha.send_text(chat_id, format_appointments_list(appts, self.tz))
            return {"status": "ok", "action": "turnos_list"}

        if cmd == "/fotos":
            return await self._handle_fotos_command(chat_id=chat_id, body_text=body_text, now=now)

        if cmd == "/turno":
            try:
                parsed = parse_appointment_command(body_text, self.tz, now=now)
            except ValueError as exc:
                await self.waha.send_text(chat_id, f"❌ {exc}")
                return {"status": "ok", "action": "turno_error"}
            appt = self.db.create_appointment(
                dog_name=parsed["dog_name"],
                checkup_type=parsed["checkup_type"],
                scheduled_at=parsed["scheduled_at"],
                location=parsed["location"],
                notes=parsed["notes"],
            )
            target_group = self.get_group_id() or chat_id
            pub = await self.publish_appointment(appt, target_chat_id=target_group)
            if target_group != chat_id:
                gcal_note = " + Google Calendar" if pub.get("gcal", {}).get("ok") else ""
                await self.waha.send_text(
                    chat_id,
                    f"✅ Turno agendado para *{appt['dog_name']}* y publicado en el grupo ({pub['mode']}{gcal_note}).",
                )
            return {"status": "ok", "action": "turno_created", "mode": pub["mode"], "gcal": pub.get("gcal")}

        await self.waha.send_text(
            chat_id,
            "Comando no reconocido. Mandá */ayuda* para ver los comandos disponibles.",
        )
        return {"status": "ok", "action": "unknown_command"}

    def _help_text(self) -> str:
        return (
            "🐾 *Bot de Pancho y Tito — Comandos*\n\n"
            "• En el horario de comida mando una *encuesta* al grupo: tocá *✋ Yo les doy* para recibir la lista por privado, o *✅ Ya les di todo* directo.\n"
            "• */dar* — Pedir la lista del turno actual por privado\n"
            "• */fotos [perro o remedio]* — Ver fotos de referencia (caja/pastilla)\n"
            "• */stock* — Ver stock y días restantes de cada remedio\n"
            "• */compre <remedio> <cantidad>* — Sumar compra al stock\n"
            "• */set <remedio> <cantidad>* — Recalibrar stock manual\n"
            "• */turno <perro> | <DD/MM HH:MM> | <tipo> | <lugar> | <notas>* — Cargar turno vet\n"
            "• */turnos* — Ver próximos turnos veterinarios\n"
            "• */grupo* — Vincular este grupo\n\n"
            f"🌐 Web UI: {self.settings.app_public_url}"
        )

    def _match_medication(self, query: str) -> tuple[dict[str, Any] | None, str | None]:
        meds = self.db.list_medications(active_only=True)
        if not meds:
            meds = self.db.list_medications(active_only=False)
        if not meds:
            return None, "❌ No hay medicamentos cargados todavía."

        norm_q = normalize_text(query)
        exact_full = [
            m for m in meds if normalize_text(f"{m['dog_name']} {m['name']}") == norm_q
        ]
        if len(exact_full) == 1:
            return exact_full[0], None

        exact_name = [m for m in meds if normalize_text(m["name"]) == norm_q]
        if len(exact_name) == 1:
            return exact_name[0], None
        if len(exact_name) > 1:
            opts = ", ".join(f"{m['dog_name']} {m['name']}" for m in exact_name)
            return None, f"⚠️ Hay varios con ese nombre ({opts}). Indicá el perro, ej: `{exact_name[0]['dog_name']} {exact_name[0]['name']}`."

        q_tokens = norm_q.split()
        token_matches = [
            m
            for m in meds
            if all(tok in normalize_text(f"{m['dog_name']} {m['name']}") for tok in q_tokens)
        ]
        if len(token_matches) == 1:
            return token_matches[0], None
        if len(token_matches) > 1:
            opts = ", ".join(f"{m['dog_name']} {m['name']}" for m in token_matches)
            return None, f"⚠️ Coincide con varios medicamentos: {opts}. Especificá cuál."

        available = ", ".join(f"{m['dog_name']} {m['name']}" for m in meds[:8])
        return None, f"❌ No encontré ningún medicamento como '{query}'. Cargados: {available}"

    def _apply_stock_command(
        self,
        body_text: str,
        *,
        mode: str,
        actor_name: str,
        today: date,
        today_str: str,
    ) -> str:
        # Remove leading command token (/compre, /compré, /set)
        parts = body_text.strip().split(None, 1)
        args = parts[1].strip() if len(parts) > 1 else ""
        match = re.match(
            r"^(.*?)\s+(\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?)$",
            args,
        )
        if not match:
            if mode == "purchase":
                return "Uso: */compre <medicamento> <cantidad>* (ej: `/compre omeprazol 30` o `/compre pancho omeprazol 15`)"
            return "Uso: */set <medicamento> <cantidad>* (ej: `/set omeprazol 12 1/2` o `/set diurene 9 3/4`)"

        query = match.group(1).strip()
        try:
            qty = parse_qty_text(match.group(2))
        except ValueError as exc:
            return f"❌ {exc}"
        if mode == "purchase" and qty <= 0:
            return "❌ La cantidad comprada debe ser mayor a 0."

        med, err = self._match_medication(query)
        if err or med is None:
            return err or "❌ Medicamento no encontrado."

        if mode == "purchase":
            updated = self.db.adjust_stock(
                med["id"], delta=qty, reason="purchase_whatsapp", user=actor_name
            )
            verb = f"+{format_qty(qty, med['unit'])}"
        else:
            updated = self.db.set_stock(
                med["id"], new_quantity=qty, reason="manual_set_whatsapp", user=actor_name
            )
            verb = f"={format_qty(qty, med['unit'])}"

        logs = self.db.get_dose_logs_for_date(today_str)
        given_meals = {m for m, row in logs.items() if row.get("given_at")}
        status = calculate_stock_status(updated, today, given_meals)
        new_qty_str = format_qty(float(updated["stock_quantity"]), updated["unit"])
        unit_str = format_unit(updated["unit"], float(updated["stock_quantity"]))

        days = status["days_stock"]
        if status["covers_until_end"]:
            coverage = f"cubre hasta el fin del tratamiento ({updated['end_date']})"
        elif days is None:
            coverage = "sin consumo diario configurado"
        else:
            coverage = f"alcanza para *{days} días*"
        return (
            f"📦 *{updated['dog_name']} — {updated['name']}* ({verb}):\n"
            f"Stock actual: *{new_qty_str} {unit_str}* → {coverage}."
        )

    # ---- Meal Lifecycle ----

    async def send_meal_reminder(
        self, date_str: str, meal: str, now_ts: int | None = None, force: bool = False
    ) -> bool:
        group_id = self.get_group_id()
        if not group_id:
            logger.warning("Cannot send meal reminder: whatsapp_group_id not configured")
            return False

        log = self.db.get_dose_log(date_str, meal)
        if not force and log and (log.get("given_at") or log.get("reminder_sent_at")):
            return False

        meds = self.db.list_medications(active_only=True)
        due_meds = get_due_meds(meds, parse_date(date_str), meal)
        fasting_offset = get_max_fasting_offset(due_meds)
        dog_names = self._active_dog_names_str()
        meal_title = MEAL_LABELS[meal]
        meal_hhmm = self._meal_time_str(meal)

        icon = "🍽️" if meal == "lunch" else "🌙"
        if fasting_offset > 0:
            poll_title = f"{icon} {meal_title} {dog_names} · ⏱️ Hay ayunas ({fasting_offset}m)"
        else:
            poll_title = f"{icon} {meal_title} {dog_names} ({meal_hhmm})"

        options = ["✋ Yo les doy", "✅ Ya les di todo"]
        poll_msg_id = await self.waha.send_poll(
            group_id, poll_title, options, multiple_answers=False
        )
        ts = now_ts if now_ts is not None else int(time.time())
        self.db.upsert_dose_log_fields(
            date_str,
            meal,
            reminder_sent_at=ts,
            group_poll_msg_id=poll_msg_id,
        )
        return True

    async def send_meal_followup(
        self, date_str: str, meal: str, now_ts: int | None = None
    ) -> bool:
        group_id = self.get_group_id()
        if not group_id:
            return False
        log = self.db.get_dose_log(date_str, meal)
        if log and (log.get("given_at") or log.get("followup_sent_at")):
            return False

        dog_names = self._active_dog_names_str()
        meal_title = MEAL_LABELS[meal]
        claimed_by = log.get("claimed_by") if log else None
        if claimed_by:
            poll_title = f"⏰ {meal_title} {dog_names} — {claimed_by} tomó el turno, ¿ya comieron?"
        else:
            poll_title = f"⏰ {meal_title} {dog_names} — ¿ya comieron?"

        options = ["✋ Yo les doy", "✅ Ya les di todo"]
        poll_msg_id = await self.waha.send_poll(
            group_id, poll_title, options, multiple_answers=False
        )
        ts = now_ts if now_ts is not None else int(time.time())
        self.db.upsert_dose_log_fields(
            date_str,
            meal,
            followup_sent_at=ts,
            group_poll_msg_id=poll_msg_id,
        )
        return True

    async def claim_meal(
        self,
        date_str: str,
        meal: str,
        actor_jid: str,
        actor_name: str,
        reply_chat_id: str | None = None,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        ts = now_ts if now_ts is not None else int(time.time())
        existing = self.db.get_dose_log(date_str, meal)
        meal_title = MEAL_LABELS[meal]
        if existing and existing.get("given_at"):
            hhmm = datetime.fromtimestamp(int(existing["given_at"]), tz=self.tz).strftime("%H:%M")
            target = reply_chat_id or actor_jid
            if target:
                await self.waha.send_text(
                    target,
                    f"✅ El {meal_title.lower()} de hoy ya lo dio *{existing['given_by']}* ({hhmm}).",
                )
            return {"status": "already_given", "log": existing}

        meds = self.db.list_medications(active_only=True)
        due_meds = get_due_meds(meds, parse_date(date_str), meal)
        dogs = self.db.list_dogs(active_only=True)
        fasting_offset = get_max_fasting_offset(due_meds)

        checklist = format_dm_checklist(date_str, meal, due_meds, dogs)
        send_mode = self.db.get_setting("send_photos_in_dm", "always")
        has_any_photo = any(self.db.list_medication_photos(m["id"]) for m in due_meds)
        if send_mode != "always" and has_any_photo:
            checklist += "\n\n📷 Mandá */fotos* si querés ver las fotos de referencia."
        await self.waha.send_text(actor_jid, checklist)

        if send_mode == "always" and due_meds:
            await self.send_med_reference_photos(actor_jid, due_meds)

        if fasting_offset > 0:
            dm_poll_title = f"📋 Checklist {meal_title}"
            dm_options = [
                f"⏱️ Ayunas dado (aviso en {fasting_offset}m)",
                "✅ Comida y meds listas",
                "❌ No puedo",
            ]
            dm_poll_id = await self.waha.send_poll(
                actor_jid, dm_poll_title, dm_options, multiple_answers=True
            )
        else:
            dm_poll_title = f"📋 Confirmar {meal_title}"
            dm_options = [
                "✅ Comida y meds listas",
                "❌ No puedo",
            ]
            dm_poll_id = await self.waha.send_poll(
                actor_jid, dm_poll_title, dm_options, multiple_answers=False
            )

        updated = self.db.upsert_dose_log_fields(
            date_str,
            meal,
            claimed_by=actor_name,
            claimed_by_jid=actor_jid,
            claimed_at=ts,
            dm_poll_msg_id=dm_poll_id,
        )

        group_id = self.get_group_id()
        if group_id:
            await self.waha.send_text(
                group_id,
                f"👍 *{actor_name}* se encarga del {meal_title.lower()}. Le mandé la lista por privado.",
            )
        return {"status": "claimed", "log": updated}

    async def send_med_reference_photos(
        self, chat_id: str, meds: list[dict[str, Any]]
    ) -> int:
        """Send medication reference photos (package/dose composite) in checklist timing order."""
        timing_order = {"fasting_before": 0, "with_food": 1, "after_food": 2}
        ordered = sorted(
            meds,
            key=lambda m: (
                timing_order.get(str(m.get("timing") or "with_food"), 1),
                str(m.get("dog_name") or "").lower(),
                str(m.get("name") or "").lower(),
            ),
        )
        sent_count = 0
        for med in ordered:
            photos = self.db.list_medication_photos(int(med["id"]))
            if not photos:
                continue
            built = build_reference_image(photos, self.photos_dir)
            if built is None:
                continue
            img_bytes, filename, label_note = built
            if med.get("due_dose") is not None and float(med["due_dose"]) > 0:
                dose = float(med["due_dose"])
                qty_str = format_qty(dose, med.get("unit"))
                unit_str = format_unit(med["unit"], dose)
                timing_txt = TIMING_LABELS.get(str(med.get("timing") or ""), "").lower()
                timing_suffix = f" ({timing_txt})" if timing_txt else ""
                caption = (
                    f"🐾 *{med['dog_name']} — {med['name']}*: *{qty_str} {unit_str}*{timing_suffix}\n"
                    f"📷 {label_note}"
                )
            else:
                caption = f"🐾 *{med['dog_name']} — {med['name']}*\n📷 {label_note}"
            try:
                await self.waha.send_image_base64(
                    chat_id=chat_id,
                    filename=filename,
                    mimetype="image/jpeg",
                    data_bytes=img_bytes,
                    caption=caption,
                )
                sent_count += 1
            except Exception as exc:
                logger.warning(
                    "Failed sending reference photo for med %s: %s", med.get("id"), exc
                )
        return sent_count

    async def _handle_fotos_command(
        self, *, chat_id: str, body_text: str, now: datetime
    ) -> dict[str, Any]:
        parts = body_text.strip().split(None, 1)
        query = parts[1].strip() if len(parts) > 1 else ""
        meds = self.db.list_medications(active_only=True)
        if not meds:
            await self.waha.send_text(chat_id, "📷 No hay medicamentos activos cargados.")
            return {"status": "ok", "action": "fotos_empty"}

        if query:
            tokens = normalize_text(query).split()
            matched = [
                m
                for m in meds
                if all(tok in normalize_text(f"{m['dog_name']} {m['name']}") for tok in tokens)
            ]
            if not matched:
                await self.waha.send_text(
                    chat_id, f"❌ No encontré medicamentos que coincidan con '{query}'."
                )
                return {"status": "ok", "action": "fotos_not_found"}
            with_photos = [m for m in matched if self.db.list_medication_photos(m["id"])]
            if not with_photos:
                names = ", ".join(f"{m['dog_name']} {m['name']}" for m in matched[:5])
                await self.waha.send_text(
                    chat_id, f"📷 Todavía no hay fotos cargadas para: {names}."
                )
                return {"status": "ok", "action": "fotos_none"}
            sent = await self.send_med_reference_photos(chat_id, with_photos)
            return {"status": "ok", "action": "fotos_query", "count": sent}

        meal = self._infer_active_meal(now)
        due_meds = get_due_meds(meds, now.date(), meal)
        target_meds = [m for m in due_meds if self.db.list_medication_photos(m["id"])]
        if not target_meds:
            target_meds = [m for m in meds if self.db.list_medication_photos(m["id"])]
        if not target_meds:
            await self.waha.send_text(
                chat_id,
                "📷 Todavía no hay fotos cargadas. Podés subirlas desde la Web UI.",
            )
            return {"status": "ok", "action": "fotos_none"}

        sent = await self.send_med_reference_photos(chat_id, target_meds)
        return {"status": "ok", "action": "fotos_sent", "count": sent}

    async def mark_fasting_done(
        self,
        date_str: str,
        meal: str,
        actor_jid: str,
        actor_name: str,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        ts = now_ts if now_ts is not None else int(time.time())
        existing = self.db.get_dose_log(date_str, meal)
        if existing and (existing.get("given_at") or existing.get("fasting_given_at")):
            return {"status": "noop", "log": existing}

        meds = self.db.list_medications(active_only=True)
        due_meds = get_due_meds(meds, parse_date(date_str), meal)
        offset = get_max_fasting_offset(due_meds) or 30
        remind_at = ts + (offset * 60)

        fields: dict[str, Any] = {
            "fasting_given_at": ts,
            "fasting_remind_at": remind_at,
            "fasting_reminded": 0,
        }
        if not existing or not existing.get("claimed_by_jid"):
            fields["claimed_by"] = actor_name
            fields["claimed_by_jid"] = actor_jid
            fields["claimed_at"] = ts

        updated = self.db.upsert_dose_log_fields(date_str, meal, **fields)
        remind_dt = datetime.fromtimestamp(remind_at, tz=self.tz)
        await self.waha.send_text(
            actor_jid,
            f"⏱️ ¡Anotado! Te aviso a las *{remind_dt.strftime('%H:%M')}* (en {offset} min) para darles la comida y el resto.",
        )
        return {"status": "scheduled", "log": updated}

    async def send_fasting_timer_dm(
        self, log_row: dict[str, Any], now_ts: int | None = None
    ) -> None:
        target_jid = log_row.get("claimed_by_jid")
        if not target_jid:
            return
        meds = self.db.list_medications(active_only=True)
        due_meds = get_due_meds(meds, parse_date(log_row["date"]), log_row["meal"])
        msg = format_step2_only(log_row["meal"], due_meds)
        await self.waha.send_text(target_jid, msg)

    async def release_meal(
        self,
        date_str: str,
        meal: str,
        actor_jid: str,
        actor_name: str,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        existing = self.db.get_dose_log(date_str, meal)
        if existing and existing.get("given_at"):
            await self.waha.send_text(
                actor_jid,
                f"✅ Ese turno ya fue confirmado por *{existing['given_by']}*.",
            )
            return {"status": "already_given", "log": existing}

        self.db.clear_claim(date_str, meal)
        await self.waha.send_text(
            actor_jid,
            "👌 Turno liberado. Aviso al grupo para que tome la posta otra persona.",
        )

        group_id = self.get_group_id()
        if group_id:
            meal_title = MEAL_LABELS[meal].lower()
            dog_names = self._active_dog_names_str()
            poll_title = f"🔄 {actor_name} liberó el {meal_title} de {dog_names} — ¿quién les da?"
            options = ["✋ Yo les doy", "✅ Ya les di todo"]
            poll_msg_id = await self.waha.send_poll(
                group_id, poll_title, options, multiple_answers=False
            )
            self.db.upsert_dose_log_fields(date_str, meal, group_poll_msg_id=poll_msg_id)
        return {"status": "released"}

    async def confirm_meal(
        self,
        date_str: str,
        meal: str,
        actor_jid: str | None,
        actor_name: str,
        from_dm: bool = False,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        ts = now_ts if now_ts is not None else int(time.time())
        target_date = parse_date(date_str)
        meds = self.db.list_medications(active_only=True)
        due_meds = get_due_meds(meds, target_date, meal)

        already_confirmed, log_row, updated_meds = self.db.confirm_meal_atomic(
            date_str=date_str,
            meal=meal,
            given_by=actor_name,
            given_by_jid=actor_jid,
            now_ts=ts,
            due_meds=due_meds,
        )
        meal_title = MEAL_LABELS[meal]

        if already_confirmed:
            if from_dm and actor_jid:
                await self.waha.send_text(
                    actor_jid,
                    f"ℹ️ El {meal_title.lower()} de hoy ya había sido confirmado por *{log_row['given_by']}*.",
                )
            return {"already_confirmed": True, "log": log_row, "updated_meds": []}

        hhmm = datetime.fromtimestamp(ts, tz=self.tz).strftime("%H:%M")
        if from_dm and actor_jid:
            await self.waha.send_text(
                actor_jid,
                f"✅ ¡Perfecto, {actor_name}! Ya registré el {meal_title.lower()} y desconté el stock.",
            )

        group_id = self.get_group_id()
        if group_id:
            await self.waha.send_text(
                group_id,
                f"✅ *{meal_title} y medicación listos* por *{actor_name}* ({hhmm})",
            )

            logs_today = self.db.get_dose_logs_for_date(date_str)
            given_meals = {m for m, row in logs_today.items() if row.get("given_at")}
            alert_lines: list[str] = []
            for med in updated_meds:
                st = calculate_stock_status(med, target_date, given_meals)
                if st["should_alert"]:
                    qty_str = format_qty(float(med["stock_quantity"]), med.get("unit"))
                    unit_str = format_unit(med["unit"], float(med["stock_quantity"]))
                    alert_lines.append(
                        f"🚨 *{med['dog_name']} — {med['name']}*: quedan {qty_str} {unit_str} (~{st['days_stock']} días)"
                    )
            if alert_lines:
                await self.waha.send_text(
                    group_id,
                    "🛒 *Alerta de compra:*\n" + "\n".join(alert_lines),
                )

        return {"already_confirmed": False, "log": log_row, "updated_meds": updated_meds}

    # ---- Appointments ----

    async def publish_appointment(
        self,
        appt: dict[str, Any],
        target_chat_id: str | None = None,
        publish_whatsapp: bool = True,
        publish_gcal: bool = True,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mode": "skipped",
            "message_id": "",
            "gcal": {"ok": False, "skipped": True},
        }
        if publish_whatsapp:
            chat_id = target_chat_id or self.get_group_id()
            if chat_id:
                try:
                    wa_res = await self.waha.publish_appointment_event(chat_id, appt, self.tz)
                    result["mode"] = wa_res.get("mode", "text")
                    result["message_id"] = wa_res.get("message_id", "")
                    if result["mode"] == "event":
                        self.db.update_appointment(appt["id"], whatsapp_event_sent=1)
                except Exception as exc:
                    logger.warning("WhatsApp publish_appointment failed: %s", exc)
                    result["mode"] = "error"
                    result["whatsapp_error"] = str(exc)
            else:
                result["mode"] = "not_sent"

        if publish_gcal and self.gcal is not None and self.gcal.is_configured():
            try:
                gcal_res = await self.gcal.create_event(appt, self.tz)
                result["gcal"] = gcal_res
                if gcal_res.get("event_id"):
                    self.db.update_appointment(
                        appt["id"], google_calendar_event_id=gcal_res["event_id"]
                    )
            except Exception as exc:
                logger.warning("Google Calendar create_event failed: %s", exc)
                result["gcal"] = {"ok": False, "error": str(exc)}
        return result

    async def send_appointment_reminder(self, appt: dict[str, Any], hours: int) -> None:
        group_id = self.get_group_id()
        if not group_id:
            return
        prefix = (
            "⏰ *Recordatorio: Turno veterinario mañana (en ~24 hs)*"
            if hours == 24
            else "🚨 *Recordatorio: Turno veterinario en ~2 hs*"
        )
        msg = format_appointment_card(appt, self.tz, prefix=prefix)
        await self.waha.send_text(group_id, msg)
