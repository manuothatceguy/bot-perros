from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import re
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo


MEAL_LABELS = {
    "lunch": "Almuerzo",
    "dinner": "Cena",
}

TIMING_LABELS = {
    "fasting_before": "En ayunas",
    "with_food": "Con la comida",
    "after_food": "Después de comer",
}


def normalize_text(value: str) -> str:
    """Lowercase, strip accents, and collapse whitespace."""
    nfd = unicodedata.normalize("NFD", value or "")
    stripped = "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.lower().strip().split())


PILL_FRACTIONS = (
    (0.125, "1/8"),
    (0.25, "1/4"),
    (1.0 / 3.0, "1/3"),
    (0.375, "3/8"),
    (0.5, "1/2"),
    (0.625, "5/8"),
    (2.0 / 3.0, "2/3"),
    (0.75, "3/4"),
    (0.875, "7/8"),
)


def extract_wa_msg_key(msg_id: str | None) -> str:
    """Extract canonical WhatsApp message key ID from 3- or 4-part serialized WAHA IDs."""
    if not msg_id:
        return ""
    raw = str(msg_id).strip()
    parts = raw.split("_")
    if len(parts) >= 3 and parts[0] in ("true", "false") and "@" in parts[1]:
        return parts[2]
    return raw


def parse_date(date_str: str) -> date:
    """Parse YYYY-MM-DD into a date object."""
    return date.fromisoformat(date_str)


def format_qty(value: float, unit: str | None = None) -> str:
    """Format medication quantities cleanly (fractions for pills, decimals otherwise)."""
    rounded = round(float(value), 4)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    if unit == "pills":
        whole = int(math.floor(rounded + 1e-9))
        frac = rounded - whole
        for target, label in PILL_FRACTIONS:
            if abs(frac - target) < 5e-4:
                return f"{whole} {label}" if whole > 0 else label
    return f"{rounded:.4f}".rstrip("0").rstrip(".")


def parse_qty_text(raw: str) -> float:
    """Parse decimal ('0.125', '0,5'), fraction ('1/8'), or mixed ('1 1/2') quantities."""
    s = " ".join((raw or "").strip().split()).replace(",", ".")
    if not s:
        raise ValueError("Cantidad vacía.")
    m_mixed = re.fullmatch(r"(\d+)\s+(\d+)/(\d+)", s)
    if m_mixed:
        whole = int(m_mixed.group(1))
        num = int(m_mixed.group(2))
        den = int(m_mixed.group(3))
        if den <= 0:
            raise ValueError("El denominador no puede ser 0.")
        return round(whole + (num / den), 4)
    m_frac = re.fullmatch(r"(\d+)/(\d+)", s)
    if m_frac:
        num = int(m_frac.group(1))
        den = int(m_frac.group(2))
        if den <= 0:
            raise ValueError("El denominador no puede ser 0.")
        return round(num / den, 4)
    m_dec = re.fullmatch(r"\d+(?:\.\d+)?", s)
    if m_dec:
        return round(float(s), 4)
    raise ValueError(f"Cantidad inválida: '{raw}'.")


def format_unit(unit: str, qty: float) -> str:
    """Format unit label in natural Spanish."""
    if unit == "pills":
        return "pastilla" if qty <= 1.0 + 1e-9 else "pastillas"
    if unit == "drops":
        return "gota" if qty <= 1.0 + 1e-9 else "gotas"
    return "ml"


def get_med_dose(med: dict[str, Any], meal: str) -> float:
    """Return dose for lunch or dinner."""
    if meal == "lunch":
        return float(med.get("dose_lunch") or 0.0)
    if meal == "dinner":
        return float(med.get("dose_dinner") or 0.0)
    return 0.0


def is_med_due_on(med: dict[str, Any], target_date: date, meal: str) -> bool:
    """Check whether medication should be administered on target_date and meal."""
    if not med.get("active", 1):
        return False
    start = parse_date(med["start_date"])
    if target_date < start:
        return False
    end_raw = med.get("end_date")
    if end_raw and target_date > parse_date(end_raw):
        return False
    interval = max(1, int(med.get("interval_days") or 1))
    if (target_date - start).days % interval != 0:
        return False
    return get_med_dose(med, meal) > 0.0


def get_due_meds(
    meds: list[dict[str, Any]], target_date: date, meal: str
) -> list[dict[str, Any]]:
    """Return list of due medication dicts enriched with 'due_dose'."""
    due: list[dict[str, Any]] = []
    for med in meds:
        if is_med_due_on(med, target_date, meal):
            item = dict(med)
            item["due_dose"] = get_med_dose(med, meal)
            due.append(item)
    return due


def get_max_fasting_offset(due_meds: list[dict[str, Any]]) -> int:
    """Return max fasting_offset_min among due fasting_before medications, or 0."""
    offsets = [
        max(0, int(m.get("fasting_offset_min") or 30))
        for m in due_meds
        if m.get("timing") == "fasting_before"
    ]
    return max(offsets) if offsets else 0


def _remaining_dose_until_end(
    med: dict[str, Any],
    today: date,
    end: date,
    given_meals_today: set[str],
) -> float:
    start = parse_date(med["start_date"])
    if end < today or end < start:
        return 0.0
    interval = max(1, int(med.get("interval_days") or 1))
    lunch_dose = float(med.get("dose_lunch") or 0.0)
    dinner_dose = float(med.get("dose_dinner") or 0.0)
    cycle_dose = lunch_dose + dinner_dose
    if cycle_dose <= 0:
        return 0.0

    cursor = max(today, start)
    offset = (cursor - start).days % interval
    if offset != 0:
        cursor = cursor + timedelta(days=(interval - offset))
    if cursor > end:
        return 0.0

    total = 0.0
    if cursor == today:
        if "lunch" not in given_meals_today:
            total += lunch_dose
        if "dinner" not in given_meals_today:
            total += dinner_dose
        cursor = cursor + timedelta(days=interval)

    if cursor <= end:
        cycles = ((end - cursor).days // interval) + 1
        total += cycles * cycle_dose
    return round(total, 4)


def calculate_stock_status(
    med: dict[str, Any],
    today: date,
    given_meals_today: set[str] | None = None,
) -> dict[str, Any]:
    """Compute days of stock, treatment expiration, and whether purchase alert applies."""
    stock_qty = float(med.get("stock_quantity") or 0.0)
    is_active = bool(med.get("active", 1))
    end_raw = med.get("end_date")
    end = parse_date(end_raw) if end_raw else None
    is_expired = bool(end is not None and today > end)
    interval = max(1, int(med.get("interval_days") or 1))
    cycle_dose = float(med.get("dose_lunch") or 0.0) + float(med.get("dose_dinner") or 0.0)
    alert_days = max(0, int(med.get("alert_days") if med.get("alert_days") is not None else 5))

    if not is_active or is_expired:
        return {
            "days_stock": None,
            "is_expired": is_expired,
            "covers_until_end": is_expired,
            "remaining_needed": 0.0,
            "should_alert": False,
        }

    days_stock: int | None
    if cycle_dose <= 0:
        days_stock = None
    else:
        cycles_covered = math.floor((stock_qty + 1e-9) / cycle_dose)
        days_stock = max(0, cycles_covered * interval)

    remaining_needed = 0.0
    covers_until_end = False
    if end is not None:
        remaining_needed = _remaining_dose_until_end(
            med, today, end, given_meals_today or set()
        )
        covers_until_end = (stock_qty + 1e-9) >= remaining_needed

    should_alert = (
        days_stock is not None
        and not covers_until_end
        and days_stock <= alert_days
    )
    return {
        "days_stock": days_stock,
        "is_expired": False,
        "covers_until_end": covers_until_end,
        "remaining_needed": remaining_needed,
        "should_alert": should_alert,
    }


def _group_by_dog(meds: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for med in meds:
        dog = med["dog_name"]
        grouped.setdefault(dog, []).append(med)
    return list(grouped.items())


def _format_med_line(med: dict[str, Any]) -> str:
    dose = float(med["due_dose"])
    qty_str = format_qty(dose, med.get("unit"))
    unit_str = format_unit(med["unit"], dose)
    notes = f" ({med['notes']})" if med.get("notes") else ""
    return f"  • *{med['name']}*: {qty_str} {unit_str}{notes}"


def format_dm_checklist(
    date_str: str,
    meal: str,
    due_meds: list[dict[str, Any]],
    dogs: list[dict[str, Any]] | None = None,
) -> str:
    """Build step-by-step DM checklist grouped by timing and dog name."""
    d = parse_date(date_str)
    meal_title = MEAL_LABELS.get(meal, meal)
    icon = "🍽️" if meal == "lunch" else "🌙"
    dog_names = ", ".join(dog["name"] for dog in dogs) if dogs else "Pancho y Tito"

    fasting = [m for m in due_meds if m.get("timing") == "fasting_before"]
    with_food = [m for m in due_meds if m.get("timing") == "with_food"]
    after_food = [m for m in due_meds if m.get("timing") == "after_food"]

    lines = [f"{icon} *{meal_title} ({d.strftime('%d/%m')}) — {dog_names}*", ""]

    if not due_meds:
        lines.append("Hoy no hay medicación programada para este turno, solo comida normal.")
        lines.append("")
        lines.append("Tocá *✅ Listo, ya les di todo* en la encuesta de abajo cuando termines.")
        return "\n".join(lines)

    if fasting:
        offset = get_max_fasting_offset(due_meds)
        lines.append(f"⏱️ *Paso 1 — En ayunas ({offset} min antes de comer):*")
        for dog, items in _group_by_dog(fasting):
            lines.append(f"🐾 *{dog}*")
            for item in items:
                lines.append(_format_med_line(item))
        lines.append("")

    if with_food:
        step_num = "Paso 2" if fasting else "Paso 1"
        suffix = f" ({get_max_fasting_offset(due_meds)} min después)" if fasting else ""
        lines.append(f"🥣 *{step_num} — Con la comida{suffix}:*")
        for dog, items in _group_by_dog(with_food):
            lines.append(f"🐾 *{dog}*")
            for item in items:
                lines.append(_format_med_line(item))
        lines.append("")
    elif fasting:
        lines.append("🥣 *Paso 2 — Comida (luego del ayuno):*")
        lines.append("• Dar comida normal (sin medicación extra con la comida).")
        lines.append("")

    if after_food:
        lines.append("💊 *Después de comer:*")
        for dog, items in _group_by_dog(after_food):
            lines.append(f"🐾 *{dog}*")
            for item in items:
                lines.append(_format_med_line(item))
        lines.append("")

    if fasting:
        lines.append("👇 Usá la encuesta de abajo para avisar el ayuno o confirmar cuando termines.")
    else:
        lines.append("👇 Tocá *✅ Comida y meds listas* abajo cuando termines.")
    return "\n".join(lines)


def format_step2_only(meal: str, due_meds: list[dict[str, Any]]) -> str:
    """Format the private timer message after fasting wait finishes."""
    meal_title = MEAL_LABELS.get(meal, meal).lower()
    with_food = [m for m in due_meds if m.get("timing") == "with_food"]
    after_food = [m for m in due_meds if m.get("timing") == "after_food"]
    lines = [f"⏰ *¡Ya pasó el tiempo de ayuno!* Ya podés darles la comida del {meal_title}.", ""]

    if with_food:
        lines.append("🥣 *Medicación con la comida:*")
        for dog, items in _group_by_dog(with_food):
            lines.append(f"🐾 *{dog}*")
            for item in items:
                lines.append(_format_med_line(item))
        lines.append("")
    if after_food:
        lines.append("💊 *Después de comer:*")
        for dog, items in _group_by_dog(after_food):
            lines.append(f"🐾 *{dog}*")
            for item in items:
                lines.append(_format_med_line(item))
        lines.append("")
    if not with_food and not after_food:
        lines.append("No tienen medicación adicional con la comida este turno.")
        lines.append("")
    lines.append("Cuando termines, tildá *✅ Comida y meds listas* en la encuesta de arriba.")
    return "\n".join(lines)


def format_stock_list(
    meds: list[dict[str, Any]],
    today: date,
    given_meals_today: set[str] | None = None,
) -> str:
    """Format /stock response sorted by lowest remaining days."""
    enriched: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for med in meds:
        status = calculate_stock_status(med, today, given_meals_today)
        if not med.get("active", 1) or status["is_expired"]:
            continue
        sort_days = status["days_stock"] if status["days_stock"] is not None else 999999
        enriched.append((sort_days, med, status))

    if not enriched:
        return "📦 No hay medicamentos activos cargados."

    enriched.sort(key=lambda x: (x[0], x[1]["dog_name"].lower(), x[1]["name"].lower()))
    lines = ["📦 *Stock de medicamentos (Pancho y Tito):*", ""]
    for _, med, status in enriched:
        qty_str = format_qty(float(med["stock_quantity"]), med.get("unit"))
        unit_str = format_unit(med["unit"], float(med["stock_quantity"]))
        days = status["days_stock"]
        if status["covers_until_end"]:
            days_txt = f"{days}d (cubre hasta fin {med['end_date']})" if days is not None else "cubre fin"
            icon = "✅"
        elif days is None:
            days_txt = "sin dosis diaria"
            icon = "ℹ️"
        else:
            days_txt = f"*{days} días*"
            icon = "🚨" if status["should_alert"] else "🟢"
        lines.append(
            f"{icon} *{med['dog_name']} — {med['name']}*: {qty_str} {unit_str} → {days_txt}"
        )
    return "\n".join(lines)


def format_appointment_card(appt: dict[str, Any], tz: ZoneInfo, prefix: str = "📅 *Turno Veterinario*") -> str:
    """Format an appointment summary for WhatsApp."""
    dt = datetime.fromtimestamp(int(appt["scheduled_at"]), tz=tz)
    lines = [
        prefix,
        f"🐾 *Perro:* {appt['dog_name']}",
        f"🩺 *Control:* {appt['checkup_type']}",
        f"🗓️ *Fecha:* {dt.strftime('%d/%m/%Y %H:%M')} hs",
    ]
    if appt.get("location"):
        lines.append(f"📍 *Lugar:* {appt['location']}")
    if appt.get("notes"):
        lines.append(f"📝 *Notas:* {appt['notes']}")
    return "\n".join(lines)


def format_appointments_list(appointments: list[dict[str, Any]], tz: ZoneInfo) -> str:
    """Format upcoming appointments for /turnos."""
    if not appointments:
        return "📅 No hay turnos veterinarios próximos cargados."
    lines = ["📅 *Próximos turnos veterinarios:*", ""]
    for appt in appointments:
        dt = datetime.fromtimestamp(int(appt["scheduled_at"]), tz=tz)
        loc = f" · 📍 {appt['location']}" if appt.get("location") else ""
        notes = f" ({appt['notes']})" if appt.get("notes") else ""
        lines.append(
            f"• *{dt.strftime('%d/%m %H:%M')}* — *{appt['dog_name']}*: {appt['checkup_type']}{loc}{notes}"
        )
    return "\n".join(lines)


def _ics_escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def build_ics_content(appt: dict[str, Any], tz: ZoneInfo) -> bytes:
    """Build a standard RFC 5545 .ics calendar event with 24h and 2h VALARM reminders."""
    dt_start = datetime.fromtimestamp(int(appt["scheduled_at"]), tz=tz).astimezone(ZoneInfo("UTC"))
    dt_end = dt_start + timedelta(hours=1)
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    summary = f"Vet {appt['dog_name']}: {appt['checkup_type']}"
    desc_parts = [f"Perro: {appt['dog_name']}", f"Control: {appt['checkup_type']}"]
    if appt.get("notes"):
        desc_parts.append(f"Notas: {appt['notes']}")
    uid = f"turno-{appt['id']}-{int(appt['scheduled_at'])}@bot-perros.local"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//bot-perros//Pancho y Tito//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{dt_start.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{dt_end.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(chr(10).join(desc_parts))}",
    ]
    if appt.get("location"):
        lines.append(f"LOCATION:{_ics_escape(appt['location'])}")
    lines.extend(
        [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Recordatorio turno veterinario (24h)",
            "TRIGGER:-P1D",
            "END:VALARM",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:Recordatorio turno veterinario (2h)",
            "TRIGGER:-PT2H",
            "END:VALARM",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    return "\r\n".join(lines).encode("utf-8")


def parse_appointment_datetime(raw: str, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Parse common Spanish/ISO appointment date+time strings into timezone-aware datetime."""
    ref = now or datetime.now(tz=tz)
    clean = " ".join(raw.strip().split())
    clean = re.sub(r"\s*hs?$", "", clean, flags=re.IGNORECASE).strip()

    formats_with_year = (
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%d/%m/%y %H:%M",
    )
    for fmt in formats_with_year:
        try:
            naive = datetime.strptime(clean, fmt)
            return naive.replace(tzinfo=tz)
        except ValueError:
            continue

    formats_no_year = ("%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M")
    for fmt in formats_no_year:
        if " " not in clean:
            break
        date_part, time_part = clean.split(" ", 1)
        sep = "/" if fmt.startswith("%d/") else "-"
        if sep not in date_part:
            continue
        try:
            candidate = datetime.strptime(f"{date_part}{sep}{ref.year} {time_part}", fmt).replace(tzinfo=tz)
            if candidate < (ref - timedelta(days=1)):
                candidate = datetime.strptime(
                    f"{date_part}{sep}{ref.year + 1} {time_part}", fmt
                ).replace(tzinfo=tz)
            return candidate
        except ValueError:
            continue

    raise ValueError(
        "Formato de fecha inválido. Usá DD/MM HH:MM o DD/MM/AAAA HH:MM (ej. 28/11 16:30)."
    )


def parse_appointment_command(
    text: str, tz: ZoneInfo, now: datetime | None = None
) -> dict[str, Any]:
    """Parse '/turno <perro> | <fecha hora> | <tipo> | <lugar> | <notas>'."""
    body = re.sub(r"^/turno\b", "", text.strip(), flags=re.IGNORECASE).strip()
    parts = [p.strip() for p in body.split("|")]
    if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
        raise ValueError(
            "Uso: /turno <perro> | <DD/MM HH:MM> | <tipo> | <lugar> | <notas>\n"
            "Ej: /turno Pancho | 25/11 16:30 | Cardiología | Vet del Parque | Ir en ayunas"
        )
    dog_name = parts[0]
    dt = parse_appointment_datetime(parts[1], tz=tz, now=now)
    checkup_type = parts[2]
    location = parts[3] if len(parts) > 3 else ""
    notes = parts[4] if len(parts) > 4 else ""
    return {
        "dog_name": dog_name,
        "scheduled_at": int(dt.timestamp()),
        "checkup_type": checkup_type,
        "location": location,
        "notes": notes,
    }
