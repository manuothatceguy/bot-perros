from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.logic import (
    build_ics_content,
    calculate_stock_status,
    extract_wa_msg_key,
    format_dm_checklist,
    format_qty,
    format_stock_list,
    format_unit,
    get_due_meds,
    get_max_fasting_offset,
    is_med_due_on,
    parse_appointment_command,
    parse_qty_text,
)


TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def test_fractional_formatting():
    assert format_qty(0.25) == "0.25"
    assert format_qty(0.5) == "0.5"
    assert format_qty(1.0) == "1"
    assert format_qty(1.5) == "1.5"
    assert format_qty(2.0) == "2"
    assert format_qty(0.5, "ml") == "0.5"
    assert format_qty(2.5, "ml") == "2.5"
    assert format_qty(0.125, "pills") == "1/8"
    assert format_qty(0.25, "pills") == "1/4"
    assert format_qty(0.5, "pills") == "1/2"
    assert format_qty(0.75, "pills") == "3/4"
    assert format_qty(1.5, "pills") == "1 1/2"
    assert format_qty(2.25, "pills") == "2 1/4"
    assert format_qty(9.75, "pills") == "9 3/4"
    assert parse_qty_text("1/8") == 0.125
    assert parse_qty_text("1 1/2") == 1.5
    assert parse_qty_text("0,25") == 0.25
    with pytest.raises(ValueError):
        parse_qty_text("1/0")
    with pytest.raises(ValueError):
        parse_qty_text("abc")
    assert extract_wa_msg_key("true_120363@g.us_3EB0ABC") == "3EB0ABC"
    assert extract_wa_msg_key("true_120363@g.us_3EB0ABC_678698@lid") == "3EB0ABC"
    assert extract_wa_msg_key("3EB0ABC") == "3EB0ABC"
    assert format_unit("pills", 0.5) == "pastilla"
    assert format_unit("pills", 1.0) == "pastilla"
    assert format_unit("pills", 1.5) == "pastillas"
    assert format_unit("drops", 1.0) == "gota"
    assert format_unit("drops", 3.0) == "gotas"
    assert format_unit("ml", 2.5) == "ml"


def test_med_schedule_interval_and_end_date():
    med_periodic = {
        "id": 1,
        "dog_name": "Pancho",
        "name": "Bravecto",
        "unit": "pills",
        "dose_lunch": 1.0,
        "dose_dinner": 0.0,
        "timing": "with_food",
        "fasting_offset_min": 30,
        "interval_days": 7,
        "start_date": "2026-11-01",
        "end_date": "2026-11-15",
        "active": 1,
    }
    assert not is_med_due_on(med_periodic, date(2026, 10, 31), "lunch")
    assert is_med_due_on(med_periodic, date(2026, 11, 1), "lunch")
    assert not is_med_due_on(med_periodic, date(2026, 11, 1), "dinner")
    assert not is_med_due_on(med_periodic, date(2026, 11, 2), "lunch")
    assert is_med_due_on(med_periodic, date(2026, 11, 8), "lunch")
    assert is_med_due_on(med_periodic, date(2026, 11, 15), "lunch")
    assert not is_med_due_on(med_periodic, date(2026, 11, 22), "lunch")


def test_fasting_offset_calculation():
    meds = [
        {"timing": "with_food", "fasting_offset_min": 30},
        {"timing": "fasting_before", "fasting_offset_min": 30},
        {"timing": "fasting_before", "fasting_offset_min": 45},
    ]
    assert get_max_fasting_offset(meds) == 45
    assert get_max_fasting_offset([{"timing": "with_food", "fasting_offset_min": 30}]) == 0


def test_stock_days_daily_periodic_and_end_date_suppression():
    # Daily fractional dose: 0.25 lunch + 0.25 dinner = 0.5/day; stock 1.5 -> 3 days
    daily_med = {
        "dog_name": "Tito",
        "name": "Enalapril",
        "unit": "pills",
        "dose_lunch": 0.25,
        "dose_dinner": 0.25,
        "interval_days": 1,
        "start_date": "2026-11-01",
        "end_date": None,
        "stock_quantity": 1.5,
        "alert_days": 5,
        "active": 1,
    }
    st = calculate_stock_status(daily_med, date(2026, 11, 21))
    assert st["days_stock"] == 3
    assert st["should_alert"] is True

    # Periodic every 3 days: 0.5/cycle, stock 1.5 -> 3 cycles * 3 = 9 days
    periodic_med = {
        "dog_name": "Pancho",
        "name": "Protector",
        "unit": "pills",
        "dose_lunch": 0.5,
        "dose_dinner": 0.0,
        "interval_days": 3,
        "start_date": "2026-11-21",
        "end_date": None,
        "stock_quantity": 1.5,
        "alert_days": 5,
        "active": 1,
    }
    st_p = calculate_stock_status(periodic_med, date(2026, 11, 21))
    assert st_p["days_stock"] == 9
    assert st_p["should_alert"] is False

    # Short treatment ending tomorrow: needs 2 more doses (today dinner + tomorrow lunch+dinner = 1.5),
    # stock = 1.5 covers until end -> must NOT trigger purchase alert even though days_stock (1) <= alert_days (5)!
    short_med = {
        "dog_name": "Pancho",
        "name": "Antibiótico",
        "unit": "pills",
        "dose_lunch": 0.5,
        "dose_dinner": 0.5,
        "interval_days": 1,
        "start_date": "2026-11-18",
        "end_date": "2026-11-22",
        "stock_quantity": 1.5,
        "alert_days": 5,
        "active": 1,
    }
    st_short = calculate_stock_status(
        short_med, date(2026, 11, 21), given_meals_today={"lunch"}
    )
    assert st_short["days_stock"] == 1
    assert st_short["remaining_needed"] == 1.5
    assert st_short["covers_until_end"] is True
    assert st_short["should_alert"] is False

    # Expired treatment
    expired_med = dict(short_med, end_date="2026-11-20", stock_quantity=0.0)
    st_exp = calculate_stock_status(expired_med, date(2026, 11, 21))
    assert st_exp["is_expired"] is True
    assert st_exp["should_alert"] is False


def test_dm_checklist_groups_by_timing_and_dog():
    meds = [
        {
            "id": 1,
            "dog_name": "Pancho",
            "name": "Omeprazol",
            "unit": "pills",
            "dose_lunch": 0.5,
            "dose_dinner": 0.0,
            "timing": "fasting_before",
            "fasting_offset_min": 30,
            "interval_days": 1,
            "start_date": "2026-11-01",
            "end_date": None,
            "active": 1,
            "notes": "",
        },
        {
            "id": 2,
            "dog_name": "Tito",
            "name": "Jarabe Renal",
            "unit": "ml",
            "dose_lunch": 2.5,
            "dose_dinner": 2.5,
            "timing": "with_food",
            "fasting_offset_min": 30,
            "interval_days": 1,
            "start_date": "2026-11-01",
            "end_date": None,
            "active": 1,
            "notes": "agitar antes",
        },
    ]
    due = get_due_meds(meds, date(2026, 11, 21), "lunch")
    text = format_dm_checklist("2026-11-21", "lunch", due, [{"name": "Pancho"}, {"name": "Tito"}])
    assert "Paso 1 — En ayunas (30 min antes de comer)" in text
    assert "Pancho" in text
    assert "Omeprazol*: 1/2 pastilla" in text
    assert "Paso 2 — Con la comida (30 min después)" in text
    assert "Tito" in text
    assert "Jarabe Renal*: 2.5 ml (agitar antes)" in text


def test_parse_appointment_command_and_ics():
    ref = datetime(2026, 11, 21, 18, 0, tzinfo=TZ)
    parsed = parse_appointment_command(
        "/turno Tito | 28/11 16:30 | Cardiología | Vet del Parque | Ir en ayunas",
        TZ,
        now=ref,
    )
    assert parsed["dog_name"] == "Tito"
    assert parsed["checkup_type"] == "Cardiología"
    assert parsed["location"] == "Vet del Parque"
    assert parsed["notes"] == "Ir en ayunas"
    dt = datetime.fromtimestamp(parsed["scheduled_at"], tz=TZ)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 11, 28, 16, 30)

    ics = build_ics_content({"id": 7, **parsed}, TZ).decode("utf-8")
    assert "BEGIN:VCALENDAR" in ics
    assert "BEGIN:VEVENT" in ics
    assert "SUMMARY:Vet Tito: Cardiología" in ics
    assert "TRIGGER:-P1D" in ics
    assert "TRIGGER:-PT2H" in ics


def test_stock_list_orders_by_lowest_days():
    meds = [
        {
            "dog_name": "Pancho",
            "name": "Baja",
            "unit": "pills",
            "dose_lunch": 1.0,
            "dose_dinner": 0.0,
            "interval_days": 1,
            "start_date": "2026-11-01",
            "end_date": None,
            "stock_quantity": 2.0,
            "alert_days": 5,
            "active": 1,
        },
        {
            "dog_name": "Tito",
            "name": "Alta",
            "unit": "ml",
            "dose_lunch": 1.0,
            "dose_dinner": 0.0,
            "interval_days": 1,
            "start_date": "2026-11-01",
            "end_date": None,
            "stock_quantity": 20.0,
            "alert_days": 5,
            "active": 1,
        },
    ]
    txt = format_stock_list(meds, date(2026, 11, 21))
    assert txt.index("Baja") < txt.index("Alta")
