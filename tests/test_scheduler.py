from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.bot import DogBot
from app.config import Settings
from app.db import Database
from app.scheduler import BotScheduler
from tests.conftest import FakeWahaClient


TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@pytest.mark.asyncio
async def test_meal_reminder_fasting_lead_time_and_followup(
    db: Database, fake_waha: FakeWahaClient, bot: DogBot
):
    sched = BotScheduler(db=db, bot=bot, tz=TZ)
    day_str = "2026-11-21"

    # Add a fasting medication for lunch (30 min before 12:00 -> triggers at 11:30)
    db.create_medication(
        dog_name="Pancho",
        name="Omeprazol",
        unit="pills",
        dose_lunch=0.5,
        dose_dinner=0.0,
        timing="fasting_before",
        fasting_offset_min=30,
        start_date=day_str,
        stock_quantity=10.0,
    )

    # At 11:25 -> nothing fires yet
    s1 = await sched.tick(datetime(2026, 11, 21, 11, 25, tzinfo=TZ))
    assert s1["meal_reminders"] == []

    # At 11:30 -> lunch reminder fires with fasting alert
    s2 = await sched.tick(datetime(2026, 11, 21, 11, 30, tzinfo=TZ))
    assert s2["meal_reminders"] == ["lunch"]
    assert len(fake_waha.sent_polls) == 1
    assert "Hay ayunas (30m)" in fake_waha.sent_polls[0]["name"]

    # Repeated tick at 11:35 -> does NOT duplicate
    s3 = await sched.tick(datetime(2026, 11, 21, 11, 35, tzinfo=TZ))
    assert s3["meal_reminders"] == []

    # At 14:00 -> follow-up fires because lunch wasn't confirmed
    s4 = await sched.tick(datetime(2026, 11, 21, 14, 0, tzinfo=TZ))
    assert s4["meal_followups"] == ["lunch"]
    assert len(fake_waha.sent_polls) == 2

    # Dinner at 19:59 -> not yet (no fasting dinner med)
    s5 = await sched.tick(datetime(2026, 11, 21, 19, 59, tzinfo=TZ))
    assert s5["meal_reminders"] == []

    # Dinner at 20:00 -> fires
    s6 = await sched.tick(datetime(2026, 11, 21, 20, 0, tzinfo=TZ))
    assert s6["meal_reminders"] == ["dinner"]

    # Confirm dinner before 22:00 -> follow-up at 22:00 must NOT fire
    await bot.confirm_meal(day_str, "dinner", "549111234@c.us", "Manu", now_ts=int(datetime(2026, 11, 21, 20, 30, tzinfo=TZ).timestamp()))
    s7 = await sched.tick(datetime(2026, 11, 21, 22, 0, tzinfo=TZ))
    assert s7["meal_followups"] == []


@pytest.mark.asyncio
async def test_persisted_fasting_dm_timer_survives_restart(
    db: Database, fake_waha: FakeWahaClient, settings: Settings
):
    day_str = "2026-11-21"
    db.create_medication(
        dog_name="Tito",
        name="Sucralfato",
        unit="ml",
        dose_lunch=3.0,
        dose_dinner=0.0,
        timing="fasting_before",
        fasting_offset_min=30,
        start_date=day_str,
        stock_quantity=30.0,
    )
    db.create_medication(
        dog_name="Tito",
        name="Enalapril",
        unit="pills",
        dose_lunch=0.5,
        dose_dinner=0.0,
        timing="with_food",
        start_date=day_str,
        stock_quantity=10.0,
    )

    t0 = datetime(2026, 11, 21, 11, 32, tzinfo=TZ)
    bot1 = DogBot(db=db, waha=fake_waha, settings=settings)  # type: ignore[arg-type]
    await bot1.mark_fasting_done(day_str, "lunch", "54911999111@c.us", "Flor", now_ts=int(t0.timestamp()))

    # Simulate service restart with a brand new DogBot + BotScheduler using same SQLite DB
    bot2 = DogBot(db=db, waha=fake_waha, settings=settings)  # type: ignore[arg-type]
    sched2 = BotScheduler(db=db, bot=bot2, tz=TZ)

    # At +29 min -> not yet
    s_early = await sched2.tick(t0 + timedelta(minutes=29))
    assert s_early["fasting_reminders"] == []

    # At +30 min -> fires Paso 2 DM to Flor
    s_due = await sched2.tick(t0 + timedelta(minutes=30))
    assert s_due["fasting_reminders"] == [f"{day_str}:lunch"]
    assert any("Ya pasó el tiempo de ayuno" in t["text"] and "Enalapril" in t["text"] for t in fake_waha.sent_texts if t["chat_id"] == "54911999111@c.us")

    # Subsequent tick does not duplicate
    s_again = await sched2.tick(t0 + timedelta(minutes=31))
    assert s_again["fasting_reminders"] == []


@pytest.mark.asyncio
async def test_appointment_24h_and_2h_reminders(
    db: Database, fake_waha: FakeWahaClient, bot: DogBot
):
    sched = BotScheduler(db=db, bot=bot, tz=TZ)
    appt_dt = datetime(2026, 11, 25, 16, 0, tzinfo=TZ)
    appt = db.create_appointment(
        dog_name="Pancho",
        checkup_type="Cardiología",
        location="Vet Central",
        scheduled_at=int(appt_dt.timestamp()),
        notes="Llevar estudios",
    )

    # 25 hours before -> neither fires
    r0 = await sched.tick(appt_dt - timedelta(hours=25))
    assert r0["appt_24h"] == [] and r0["appt_2h"] == []

    # 23.5 hours before -> 24h reminder fires once
    r1 = await sched.tick(appt_dt - timedelta(hours=23, minutes=30))
    assert r1["appt_24h"] == [appt["id"]]
    assert r1["appt_2h"] == []

    # Repeat tick -> no duplicate
    r2 = await sched.tick(appt_dt - timedelta(hours=20))
    assert r2["appt_24h"] == []

    # 1h 50m before -> 2h reminder fires once
    r3 = await sched.tick(appt_dt - timedelta(hours=1, minutes=50))
    assert r3["appt_2h"] == [appt["id"]]

    r4 = await sched.tick(appt_dt - timedelta(hours=1))
    assert r4["appt_2h"] == []
