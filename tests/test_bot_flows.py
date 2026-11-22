from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.bot import DogBot
from app.db import Database
from tests.conftest import FakeWahaClient


TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def _today_iso(bot: DogBot) -> str:
    return datetime.now(tz=bot.tz).date().isoformat()


@pytest.mark.asyncio
async def test_full_poll_flow_fasting_multiselect_dedup_and_low_stock(
    db: Database, fake_waha: FakeWahaClient, bot: DogBot
):
    today = _today_iso(bot)
    # Pancho fasting daily 0.5 pill (stock 2.0 -> after 1 dose: 1.5 -> 3 days <= alert_days 5 -> triggers alert!)
    med_fasting = db.create_medication(
        dog_name="Pancho",
        name="Omeprazol",
        unit="pills",
        dose_lunch=0.5,
        dose_dinner=0.0,
        timing="fasting_before",
        fasting_offset_min=30,
        interval_days=1,
        start_date=today,
        stock_quantity=2.0,
        alert_days=5,
    )
    # Tito with_food every 2 days starting yesterday (NOT due today!)
    med_off_day = db.create_medication(
        dog_name="Tito",
        name="Suplemento",
        unit="ml",
        dose_lunch=2.0,
        dose_dinner=0.0,
        timing="with_food",
        interval_days=2,
        start_date="2020-01-01" if (datetime.now(tz=TZ).date().toordinal() % 2 == 0) else "2020-01-02",
        stock_quantity=10.0,
        alert_days=2,
    )
    # Confirm med_off_day really isn't due today by shifting start_date if needed:
    from app.logic import is_med_due_on, parse_date
    if is_med_due_on(med_off_day, parse_date(today), "lunch"):
        med_off_day = db.update_medication(med_off_day["id"], start_date="2020-01-02")  # flip parity

    # 1. Send meal reminder poll to group
    sent = await bot.send_meal_reminder(today, "lunch")
    assert sent is True
    group_poll = fake_waha.sent_polls[-1]
    assert "Hay ayunas (30m)" in group_poll["name"]
    assert "Pancho y Tito" in group_poll["name"]

    # 2. Flor (@lid resolved via family_members / WAHA lids API) votes "✋ Yo les doy"
    #    Include WAHA NOWEB's 4-segment poll ID (<poll_id>_<bot_lid>) to test key matching!
    fake_waha.lids_map["999111@lid"] = "54911999111@c.us"
    fake_waha.contacts_map["54911999111@c.us"] = "Flor"
    res = await bot.handle_webhook(
        {
            "event": "poll.vote",
            "payload": {
                "poll": {"id": f"{group_poll['id']}_67869878567122@lid", "to": "120363000000001@g.us"},
                "vote": {
                    "id": "v1",
                    "from": "999111@lid",
                    "selectedOptions": ["✋ Yo les doy"],
                    "timestamp": 1700000001,
                },
            },
        }
    )
    assert res["action"] == "claim_group_poll"
    # DM checklist + DM multi-select poll sent to @c.us
    dm_texts = [t for t in fake_waha.sent_texts if t["chat_id"] == "54911999111@c.us"]
    assert any("Paso 1 — En ayunas" in t["text"] and "Omeprazol" in t["text"] for t in dm_texts)
    dm_poll = fake_waha.sent_polls[-1]
    assert dm_poll["chat_id"] == "54911999111@c.us"
    assert dm_poll["multiple_answers"] is True

    # 3. Flor checks "⏱️ Di lo de ayunas" in DM poll
    res2 = await bot.handle_webhook(
        {
            "event": "poll.vote",
            "payload": {
                "poll": {"id": dm_poll["id"], "to": "54911999111@c.us"},
                "vote": {
                    "id": "v2",
                    "from": "54911999111@c.us",
                    "selectedOptions": [dm_poll["options"][0]],
                    "timestamp": 1700000100,
                },
            },
        }
    )
    assert res2["action"] == "fasting_dm_poll"
    log_mid = db.get_dose_log(today, "lunch")
    assert log_mid["fasting_given_at"] is not None
    assert log_mid["fasting_remind_at"] == log_mid["fasting_given_at"] + 1800

    # 4. Flor later checks "✅ Di comida y medicación" in same multi-select DM poll
    #    (WhatsApp sends both previously selected and newly selected options!)
    res3 = await bot.handle_webhook(
        {
            "event": "poll.vote",
            "payload": {
                "poll": {"id": dm_poll["id"], "to": "54911999111@c.us"},
                "vote": {
                    "id": "v3",
                    "from": "54911999111@c.us",
                    "selectedOptions": [dm_poll["options"][0], dm_poll["options"][1]],
                    "timestamp": 1700002000,
                },
            },
        }
    )
    assert res3["action"] == "confirm_dm_poll"

    fresh_fasting = db.get_medication(med_fasting["id"])
    fresh_off = db.get_medication(med_off_day["id"])
    assert fresh_fasting["stock_quantity"] == 1.5
    assert fresh_off["stock_quantity"] == 10.0  # untouched!
    assert any("Almuerzo y medicación listos* por *Flor*" in t["text"] for t in fake_waha.sent_texts if t["chat_id"] == "120363000000001@g.us")
    assert any("Alerta de compra" in t["text"] and "Omeprazol" in t["text"] for t in fake_waha.sent_texts if t["chat_id"] == "120363000000001@g.us")

    # 5. Duplicate attempt by Manu on group poll -> no second stock deduction!
    res4 = await bot.handle_webhook(
        {
            "event": "poll.vote",
            "payload": {
                "poll": {"id": group_poll["id"], "to": "120363000000001@g.us"},
                "vote": {
                    "id": "v4",
                    "from": "54911888222@c.us",
                    "selectedOptions": ["✅ Ya les di todo"],
                    "timestamp": 1700002100,
                },
            },
        }
    )
    assert res4["action"] == "confirm_group_poll"
    assert db.get_medication(med_fasting["id"])["stock_quantity"] == 1.5
    movements = db.list_stock_movements(med_id=med_fasting["id"])
    dose_moves = [m for m in movements if m["reason"].startswith("dose:")]
    assert len(dose_moves) == 1


@pytest.mark.asyncio
async def test_release_claim_and_direct_group_confirm(
    db: Database, fake_waha: FakeWahaClient, bot: DogBot
):
    today = _today_iso(bot)
    await bot.send_meal_reminder(today, "dinner")
    poll1 = fake_waha.sent_polls[-1]

    # Claim via reaction ✋ with participantAlt
    res_claim = await bot.handle_webhook(
        {
            "event": "message.reaction",
            "payload": {
                "id": "r1",
                "from": "120363000000001@g.us",
                "participant": "111222@lid",
                "notifyName": "Sofi",
                "_data": {"key": {"participantAlt": "54911444555@c.us"}},
                "reaction": {"text": "✋", "messageId": poll1["id"]},
            },
        }
    )
    assert res_claim["action"] == "claim_group_reaction"
    dm_poll = fake_waha.sent_polls[-1]

    # Release via ❌ vote in DM
    res_rel = await bot.handle_webhook(
        {
            "event": "poll.vote",
            "payload": {
                "poll": {"id": dm_poll["id"], "to": "54911444555@c.us"},
                "vote": {
                    "id": "v_rel",
                    "from": "54911444555@c.us",
                    "selectedOptions": ["❌ No puedo"],
                    "timestamp": 1700003000,
                },
            },
        }
    )
    assert res_rel["action"] == "release_dm_poll"
    log = db.get_dose_log(today, "dinner")
    assert log["claimed_by"] is None

    # New group poll was posted; Manu confirms directly on it
    poll2 = fake_waha.sent_polls[-1]
    assert "liberó el cena" in poll2["name"]
    await bot.handle_webhook(
        {
            "event": "poll.vote",
            "payload": {
                "poll": {"id": poll2["id"], "to": "120363000000001@g.us"},
                "vote": {
                    "id": "v_dir",
                    "from": "54911777888@c.us",
                    "selectedOptions": ["✅ Ya les di todo"],
                    "timestamp": 1700003100,
                },
            },
        }
    )
    assert db.get_dose_log(today, "dinner")["given_at"] is not None


@pytest.mark.asyncio
async def test_casual_chat_does_not_trigger_and_echo_prevention(
    db: Database, fake_waha: FakeWahaClient, bot: DogBot
):
    today = _today_iso(bot)
    await bot.send_meal_reminder(today, "lunch")

    # Casual message containing "yo" -> must NOT claim
    res_casual = await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "false_g_C1",
                "from": "120363000000001@g.us",
                "participant": "549111234@c.us",
                "notifyName": "Manu",
                "body": "yo voy al super en un rato",
            },
        }
    )
    assert res_casual["status"] == "ignored"
    assert db.get_dose_log(today, "lunch")["claimed_by"] is None

    # Exact "yo" -> claims!
    res_exact = await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "false_g_C2",
                "from": "120363000000001@g.us",
                "participant": "549111234@c.us",
                "notifyName": "Manu",
                "body": "yo",
            },
        }
    )
    assert res_exact["action"] == "claim_group_text"

    # Bot sent echo must be ignored even if body starts with /
    bot_msg_id = await fake_waha.send_text("120363000000001@g.us", "/stock")
    res_echo = await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": bot_msg_id,
                "from": "120363000000001@g.us",
                "fromMe": True,
                "body": "/stock",
            },
        }
    )
    assert res_echo["reason"] == "bot_sent_echo"

    # User-authored fromMe=True message (not in sent_ids) MUST be processed!
    res_from_me = await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "true_120363000000001@g.us_USERCMD1",
                "from": "120363000000001@g.us",
                "participant": "549111234@c.us",
                "fromMe": True,
                "body": "/stock",
            },
        }
    )
    assert res_from_me["action"] == "stock"


@pytest.mark.asyncio
async def test_whatsapp_commands_stock_compre_set_turno_and_autobind(
    db: Database, fake_waha: FakeWahaClient, bot: DogBot
):
    today = _today_iso(bot)
    # Clear configured group ID to test auto-bind by group name "Perros"
    db.set_setting("whatsapp_group_id", "")

    db.create_medication(
        dog_name="Pancho",
        name="Omeprazol",
        unit="pills",
        dose_lunch=1.0,
        dose_dinner=0.0,
        start_date=today,
        stock_quantity=5.0,
    )
    db.create_medication(
        dog_name="Tito",
        name="Omeprazol",
        unit="pills",
        dose_lunch=0.5,
        dose_dinner=0.0,
        start_date=today,
        stock_quantity=4.0,
    )

    # Ambiguous /compre -> asks to specify dog
    await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "m_amb",
                "from": "120363000000001@g.us",
                "participant": "549111234@c.us",
                "notifyName": "Manu",
                "body": "/compre omeprazol 10",
            },
        }
    )
    assert db.get_setting("whatsapp_group_id") == "120363000000001@g.us"
    assert "Indicá el perro" in fake_waha.sent_texts[-1]["text"]

    # Disambiguated /compre pancho omeprazol 10
    await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "m_buy",
                "from": "120363000000001@g.us",
                "participant": "549111234@c.us",
                "notifyName": "Manu",
                "body": "/compre pancho omeprazol 10",
            },
        }
    )
    assert "15 pastillas" in fake_waha.sent_texts[-1]["text"]

    # /set tito omeprazol 7,5 -> formatted as 7 1/2 pastillas
    await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "m_set",
                "from": "120363000000001@g.us",
                "participant": "549111234@c.us",
                "notifyName": "Manu",
                "body": "/set tito omeprazol 7,5",
            },
        }
    )
    assert "7 1/2 pastillas" in fake_waha.sent_texts[-1]["text"]

    # /set tito omeprazol 1/8 -> accepts fraction input and displays 1/8 pastilla
    await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "m_set_frac",
                "from": "120363000000001@g.us",
                "participant": "549111234@c.us",
                "notifyName": "Manu",
                "body": "/set tito omeprazol 1/8",
            },
        }
    )
    assert "1/8 pastilla" in fake_waha.sent_texts[-1]["text"]
    assert db.get_medication(2)["stock_quantity"] == 0.125

    # /turno -> falls back to .ics file in NOWEB
    await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "m_turno",
                "from": "120363000000001@g.us",
                "participant": "549111234@c.us",
                "notifyName": "Manu",
                "body": "/turno Pancho | 29/11 17:00 | Vacuna | Vet Central | Llevar libreta",
            },
        }
    )
    assert len(fake_waha.sent_files) == 1
    assert fake_waha.sent_files[0]["filename"].endswith(".ics")
    assert b"BEGIN:VEVENT" in fake_waha.sent_files[0]["data"]

    # /turnos lists the created appointment
    await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "m_turnos",
                "from": "120363000000001@g.us",
                "participant": "549111234@c.us",
                "notifyName": "Manu",
                "body": "/turnos",
            },
        }
    )
    assert "Vacuna" in fake_waha.sent_texts[-1]["text"]


@pytest.mark.asyncio
async def test_sync_group_participants_resolves_lids_and_skips_bot(
    db: Database, fake_waha: FakeWahaClient, bot: DogBot
):
    fake_waha.session_info["me"] = {"id": "5491100000000@c.us", "lid": "100001@lid"}
    fake_waha.groups = [
        {
            "id": "120363000000001@g.us",
            "subject": "Perros",
            "participants": [
                {"id": "100001@lid", "phoneNumber": "5491100000000@s.whatsapp.net"},
                {"id": "200002@lid", "phoneNumber": "5491100000001@s.whatsapp.net"},
                {"id": "300003@lid", "phoneNumber": "5491100000002@s.whatsapp.net"},
            ],
        }
    ]
    fake_waha.contacts_map["5491100000001@c.us"] = "Ana"

    synced = await bot.sync_group_participants()
    assert synced == 2
    members = db.list_family_members()
    jids = {m["jid"]: m for m in members}
    assert "5491100000000@c.us" not in jids
    assert jids["5491100000001@c.us"]["lid"] == "200002@lid"
    assert jids["5491100000001@c.us"]["display_name"] == "Ana"
    assert jids["5491100000002@c.us"]["lid"] == "300003@lid"


@pytest.mark.asyncio
async def test_gows_group_poll_vote_routes_dm_to_voter_never_group(
    bot: DogBot, db: Database, fake_waha: FakeWahaClient
):
    """WAHA GOWS sets vote.from to the group JID and puts the voter in vote.participant / _data.Info."""
    group_id = "120363000000001@g.us"
    db.create_medication(
        dog_name="Pancho",
        name="Omeprazol",
        unit="pills",
        dose_lunch=0.5,
        dose_dinner=0.0,
        timing="fasting_before",
        fasting_offset_min=30,
        start_date="2026-04-20",
        stock_quantity=10,
    )
    fake_waha.session_info["me"] = {"id": "5491100000000@c.us", "lid": "100001@lid"}
    fake_waha.groups = [
        {
            "JID": group_id,
            "Name": "Perros",
            "Participants": [
                {"JID": "100001@lid", "PhoneNumber": "5491100000000@s.whatsapp.net", "LID": "100001@lid"},
                {"JID": "133096775114767@lid", "PhoneNumber": "5491100000002@s.whatsapp.net", "LID": "133096775114767@lid"},
            ],
        }
    ]
    assert await bot.sync_group_participants() == 1

    await bot.send_meal_reminder("2026-04-20", "lunch")
    poll_msg_id = fake_waha.sent_polls[-1]["id"]

    res = await bot.handle_webhook(
        {
            "event": "poll.vote",
            "payload": {
                "poll": {"id": poll_msg_id, "to": group_id},
                "vote": {
                    "id": f"false_{group_id}_VOTE1",
                    "from": group_id,
                    "fromMe": False,
                    "to": "5491100000000@c.us",
                    "participant": "133096775114767@lid",
                    "selectedOptions": ["✋ Yo les doy"],
                    "timestamp": 1700000001000,
                },
                "_data": {
                    "Info": {
                        "Chat": group_id,
                        "Sender": "133096775114767@lid",
                        "SenderAlt": "5491100000002@s.whatsapp.net",
                        "PushName": "Manu",
                        "IsGroup": True,
                        "IsFromMe": False,
                    }
                },
            },
        }
    )
    assert res == {"status": "ok", "action": "claim_group_poll"}

    # Checklist text + DM poll MUST go to 5491100000002@c.us, NEVER to the group
    dm_texts = [m for m in fake_waha.sent_texts if m["chat_id"] == "5491100000002@c.us"]
    assert len(dm_texts) == 1
    assert "Omeprazol" in dm_texts[0]["text"]

    dm_polls = [p for p in fake_waha.sent_polls if p["chat_id"] == "5491100000002@c.us"]
    assert len(dm_polls) == 1
    assert "Checklist Almuerzo" in dm_polls[0]["name"]

    # Group must ONLY receive the short claim announcement with the human name, NOT the checklist or group ID
    group_texts = [m for m in fake_waha.sent_texts if m["chat_id"] == group_id]
    assert len(group_texts) == 1
    assert "Manu" in group_texts[0]["text"]
    assert "120363000000001" not in group_texts[0]["text"]
    assert "Omeprazol" not in group_texts[0]["text"]
    assert len([p for p in fake_waha.sent_polls if p["chat_id"] == group_id]) == 1
