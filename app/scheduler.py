from __future__ import annotations

from datetime import datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db import Database
from app.logic import get_due_meds, get_max_fasting_offset, parse_date

if TYPE_CHECKING:
    from app.bot import DogBot


logger = logging.getLogger(__name__)


def _parse_hhmm(value: str, fallback: str) -> tuple[int, int]:
    raw = (value or fallback).strip()
    try:
        parts = raw.split(":")
        h = int(parts[0])
        m = int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    fh, fm = fallback.split(":")
    return int(fh), int(fm)


class BotScheduler:
    """Restart-safe 1-minute tick scheduler backed by SQLite state."""

    def __init__(self, db: Database, bot: DogBot, tz: ZoneInfo) -> None:
        self.db = db
        self.bot = bot
        self.tz = tz
        self._scheduler: AsyncIOScheduler | None = None
        self._last_prune_ts = 0

    def start(self) -> None:
        if self._scheduler is not None:
            return
        self._scheduler = AsyncIOScheduler(timezone=self.tz)
        self._scheduler.add_job(
            self.tick,
            "interval",
            seconds=60,
            id="bot_perros_tick",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()
        logger.info("BotScheduler started (tz=%s)", self.tz)

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    async def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """Run one deterministic check cycle for meals, fasting timers, and vet appointments."""
        current = now.astimezone(self.tz) if now else datetime.now(tz=self.tz)
        now_ts = int(current.timestamp())
        today = current.date()
        date_str = today.isoformat()
        summary: dict[str, Any] = {
            "meal_reminders": [],
            "meal_followups": [],
            "fasting_reminders": [],
            "appt_24h": [],
            "appt_2h": [],
        }

        settings = self.db.get_all_settings()
        meds = self.db.list_medications(active_only=True)

        meal_specs = (
            (
                "lunch",
                settings.get("lunch_time", "12:00"),
                settings.get("lunch_followup_time", "14:00"),
            ),
            (
                "dinner",
                settings.get("dinner_time", "20:00"),
                settings.get("dinner_followup_time", "22:00"),
            ),
        )

        for meal, time_str, followup_str in meal_specs:
            mh, mm = _parse_hhmm(time_str, "12:00" if meal == "lunch" else "20:00")
            fh, fm = _parse_hhmm(followup_str, "14:00" if meal == "lunch" else "22:00")

            meal_dt = current.replace(hour=mh, minute=mm, second=0, microsecond=0)
            followup_dt = current.replace(hour=fh, minute=fm, second=0, microsecond=0)

            due_meds = get_due_meds(meds, today, meal)
            fasting_offset = get_max_fasting_offset(due_meds)
            trigger_dt = meal_dt - timedelta(minutes=fasting_offset)

            log = self.db.get_dose_log(date_str, meal)
            already_given = bool(log and log.get("given_at"))

            if not already_given:
                reminder_sent = bool(log and log.get("reminder_sent_at"))
                if not reminder_sent and trigger_dt <= current < (meal_dt + timedelta(hours=2)):
                    try:
                        sent = await self.bot.send_meal_reminder(date_str, meal, now_ts=now_ts)
                        if sent:
                            summary["meal_reminders"].append(meal)
                    except Exception as exc:
                        logger.error("Failed meal reminder (%s): %s", meal, exc)

                followup_sent = bool(log and log.get("followup_sent_at"))
                if not followup_sent and followup_dt <= current < (followup_dt + timedelta(hours=3)):
                    try:
                        sent = await self.bot.send_meal_followup(date_str, meal, now_ts=now_ts)
                        if sent:
                            summary["meal_followups"].append(meal)
                    except Exception as exc:
                        logger.error("Failed meal followup (%s): %s", meal, exc)

        # Persisted DM fasting timers
        for due_log in self.db.claim_fasting_reminders_due(now_ts):
            try:
                await self.bot.send_fasting_timer_dm(due_log, now_ts=now_ts)
                summary["fasting_reminders"].append(f"{due_log['date']}:{due_log['meal']}")
            except Exception as exc:
                logger.error("Failed fasting DM reminder (%s): %s", due_log["id"], exc)

        # Vet appointment reminders (24h and 2h)
        due_24h, due_2h = self.db.claim_appointment_reminders(now_ts)
        for appt in due_24h:
            try:
                await self.bot.send_appointment_reminder(appt, hours=24)
                summary["appt_24h"].append(appt["id"])
            except Exception as exc:
                logger.error("Failed 24h appt reminder (%s): %s", appt["id"], exc)

        for appt in due_2h:
            try:
                await self.bot.send_appointment_reminder(appt, hours=2)
                summary["appt_2h"].append(appt["id"])
            except Exception as exc:
                logger.error("Failed 2h appt reminder (%s): %s", appt["id"], exc)

        # Periodic dedup table cleanup (every ~6h)
        if now_ts - self._last_prune_ts > 21600:
            self._last_prune_ts = now_ts
            self.db.prune_processed_events(now_ts - 172800)

        return summary
