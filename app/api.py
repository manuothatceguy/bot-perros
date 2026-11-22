from __future__ import annotations

from datetime import datetime
import time
from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.bot import DogBot
from app.config import Settings
from app.db import Database
from app.logic import (
    MEAL_LABELS,
    calculate_stock_status,
    get_due_meds,
    get_max_fasting_offset,
    normalize_text,
)
from app.photos import delete_photo_file, get_photos_dir, process_and_save_photo
from app.waha import WahaClient


router = APIRouter()


class DogCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    notes: str = ""
    active: bool = True


class DogUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    notes: str | None = None
    active: bool | None = None


class MedicationCreate(BaseModel):
    dog_id: int | None = None
    dog_name: str | None = None
    name: str = Field(min_length=1, max_length=120)
    unit: Literal["pills", "ml", "drops"] = "pills"
    dose_lunch: float = Field(default=0.0, ge=0.0)
    dose_dinner: float = Field(default=0.0, ge=0.0)
    timing: Literal["fasting_before", "with_food", "after_food"] = "with_food"
    fasting_offset_min: int = Field(default=30, ge=0, le=240)
    interval_days: int = Field(default=1, ge=1, le=365)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    stock_quantity: float = Field(default=0.0, ge=0.0)
    alert_days: int = Field(default=5, ge=0, le=365)
    active: bool = True
    notes: str = ""


class MedicationUpdate(BaseModel):
    dog_id: int | None = None
    dog_name: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    unit: Literal["pills", "ml", "drops"] | None = None
    dose_lunch: float | None = Field(default=None, ge=0.0)
    dose_dinner: float | None = Field(default=None, ge=0.0)
    timing: Literal["fasting_before", "with_food", "after_food"] | None = None
    fasting_offset_min: int | None = Field(default=None, ge=0, le=240)
    interval_days: int | None = Field(default=None, ge=1, le=365)
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    clear_end_date: bool = False
    stock_quantity: float | None = Field(default=None, ge=0.0)
    alert_days: int | None = Field(default=None, ge=0, le=365)
    active: bool | None = None
    notes: str | None = None


class StockPurchaseRequest(BaseModel):
    quantity: float = Field(gt=0.0)
    user: str = "Web"
    reason: str = "purchase_web"


class StockSetRequest(BaseModel):
    quantity: float = Field(ge=0.0)
    user: str = "Web"
    reason: str = "manual_set_web"


class MealActionRequest(BaseModel):
    given_by: str = "Web"
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class AppointmentCreate(BaseModel):
    dog_id: int | None = None
    dog_name: str = Field(min_length=1, max_length=60)
    checkup_type: str = Field(min_length=1, max_length=120)
    location: str = ""
    scheduled_at: int
    notes: str = ""
    publish_to_whatsapp: bool = True
    publish_to_gcal: bool = True


class AppointmentUpdate(BaseModel):
    dog_id: int | None = None
    dog_name: str | None = None
    checkup_type: str | None = None
    location: str | None = None
    scheduled_at: int | None = None
    notes: str | None = None


class FamilyUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


class SettingsUpdate(BaseModel):
    whatsapp_group_id: str | None = None
    whatsapp_group_name: str | None = None
    lunch_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    lunch_followup_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    dinner_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    dinner_followup_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    send_photos_in_dm: Literal["always", "on_demand"] | None = None


def _db(request: Request) -> Database:
    return request.app.state.db


def _bot(request: Request) -> DogBot:
    return request.app.state.bot


def _waha(request: Request) -> WahaClient:
    return request.app.state.waha


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _serialize_photos(med_id: int, db: Database) -> list[dict[str, Any]]:
    return [
        {
            "id": p["id"],
            "med_id": p["med_id"],
            "kind": p["kind"],
            "caption": p["caption"],
            "url": f"/api/photos/{p['id']}",
            "created_at": p["created_at"],
        }
        for p in db.list_medication_photos(med_id)
    ]


def _enrich_med(med: dict[str, Any], db: Database, settings: Settings) -> dict[str, Any]:
    today = datetime.now(tz=settings.get_tz()).date()
    logs = db.get_dose_logs_for_date(today.isoformat())
    given_meals = {m for m, row in logs.items() if row.get("given_at")}
    item = dict(med)
    item["stock_status"] = calculate_stock_status(item, today, given_meals)
    item["photos"] = _serialize_photos(int(med["id"]), db)
    return item


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    db = _db(request)
    settings = _settings(request)
    gcal = getattr(request.app.state, "gcal", None)
    return {
        "status": "ok",
        "tz": settings.tz,
        "group_id": db.get_setting("whatsapp_group_id", ""),
        "group_name": db.get_setting("whatsapp_group_name", settings.whatsapp_group_name),
        "waha_session": settings.waha_session,
        "gcal_configured": bool(gcal and gcal.is_configured()),
    }


@router.post("/webhook")
async def webhook(request: Request) -> dict[str, Any]:
    body = await request.json()
    return await _bot(request).handle_webhook(body)


@router.get("/api/dashboard")
async def get_dashboard(request: Request) -> dict[str, Any]:
    db = _db(request)
    settings = _settings(request)
    tz = settings.get_tz()
    now = datetime.now(tz=tz)
    today = now.date()
    today_str = today.isoformat()

    app_settings = db.get_all_settings()
    dogs = db.list_dogs(active_only=False)
    meds = db.list_medications(active_only=False)
    logs_today = db.get_dose_logs_for_date(today_str)
    given_meals = {m for m, row in logs_today.items() if row.get("given_at")}

    enriched_meds = []
    photos_by_med: dict[int, list[dict[str, Any]]] = {}
    for med in meds:
        item = dict(med)
        item["stock_status"] = calculate_stock_status(item, today, given_meals)
        med_photos = _serialize_photos(int(med["id"]), db)
        item["photos"] = med_photos
        photos_by_med[int(med["id"])] = med_photos
        enriched_meds.append(item)

    active_meds = [m for m in meds if m.get("active", 1)]
    today_meals: dict[str, Any] = {}
    for meal, t_key, f_key in (
        ("lunch", "lunch_time", "lunch_followup_time"),
        ("dinner", "dinner_time", "dinner_followup_time"),
    ):
        due_meds = get_due_meds(active_meds, today, meal)
        for dm in due_meds:
            dm["photos"] = photos_by_med.get(int(dm["id"]), [])
        today_meals[meal] = {
            "meal": meal,
            "label": MEAL_LABELS[meal],
            "scheduled_time": app_settings.get(t_key, "12:00" if meal == "lunch" else "20:00"),
            "followup_time": app_settings.get(f_key, "14:00" if meal == "lunch" else "22:00"),
            "fasting_offset_min": get_max_fasting_offset(due_meds),
            "due_meds": due_meds,
            "log": logs_today.get(meal),
        }

    upcoming_appts = db.list_appointments(upcoming_only=True, from_ts=int(now.timestamp()) - 3600)
    all_appts = db.list_appointments(upcoming_only=False)

    return {
        "today": today_str,
        "now_ts": int(now.timestamp()),
        "dogs": dogs,
        "today_meals": today_meals,
        "medications": enriched_meds,
        "appointments": upcoming_appts,
        "all_appointments": all_appts,
        "family": db.list_family_members(),
        "settings": app_settings,
        "stock_movements": db.list_stock_movements(limit=30),
        "recent_dose_logs": db.list_dose_logs(limit=14),
    }


# ---- Dogs ----


@router.get("/api/dogs")
async def list_dogs(request: Request) -> list[dict[str, Any]]:
    return _db(request).list_dogs(active_only=False)


@router.post("/api/dogs", status_code=201)
async def create_dog(body: DogCreate, request: Request) -> dict[str, Any]:
    try:
        return _db(request).create_dog(body.name, notes=body.notes, active=body.active)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/dogs/{dog_id}")
async def update_dog(dog_id: int, body: DogUpdate, request: Request) -> dict[str, Any]:
    try:
        updated = _db(request).update_dog(
            dog_id, name=body.name, notes=body.notes, active=body.active
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Perro no encontrado")
    return updated


@router.delete("/api/dogs/{dog_id}")
async def delete_dog(dog_id: int, request: Request) -> dict[str, bool]:
    db = _db(request)
    filenames = db.list_photo_filenames_for_dog(dog_id)
    if not db.delete_dog(dog_id):
        raise HTTPException(status_code=404, detail="Perro no encontrado")
    photos_dir = get_photos_dir(db.db_path)
    for fn in filenames:
        delete_photo_file(photos_dir, fn)
    return {"deleted": True}


# ---- Medications ----


@router.get("/api/medications")
async def list_medications(
    request: Request,
    active_only: bool = False,
    dog_id: int | None = None,
) -> list[dict[str, Any]]:
    db = _db(request)
    settings = _settings(request)
    meds = db.list_medications(active_only=active_only, dog_id=dog_id)
    return [_enrich_med(m, db, settings) for m in meds]


@router.post("/api/medications", status_code=201)
async def create_medication(body: MedicationCreate, request: Request) -> dict[str, Any]:
    db = _db(request)
    settings = _settings(request)
    try:
        med = db.create_medication(
            dog_id=body.dog_id,
            dog_name=body.dog_name,
            name=body.name,
            unit=body.unit,
            dose_lunch=body.dose_lunch,
            dose_dinner=body.dose_dinner,
            timing=body.timing,
            fasting_offset_min=body.fasting_offset_min,
            interval_days=body.interval_days,
            start_date=body.start_date,
            end_date=body.end_date,
            stock_quantity=body.stock_quantity,
            alert_days=body.alert_days,
            active=body.active,
            notes=body.notes,
            actor="Web",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _enrich_med(med, db, settings)


@router.put("/api/medications/{med_id}")
async def update_medication(
    med_id: int, body: MedicationUpdate, request: Request
) -> dict[str, Any]:
    db = _db(request)
    settings = _settings(request)
    end_date_arg: Any = None if body.clear_end_date else (body.end_date if body.end_date is not None else ...)
    try:
        med = db.update_medication(
            med_id,
            dog_id=body.dog_id,
            dog_name=body.dog_name,
            name=body.name,
            unit=body.unit,
            dose_lunch=body.dose_lunch,
            dose_dinner=body.dose_dinner,
            timing=body.timing,
            fasting_offset_min=body.fasting_offset_min,
            interval_days=body.interval_days,
            start_date=body.start_date,
            end_date=end_date_arg,
            stock_quantity=body.stock_quantity,
            alert_days=body.alert_days,
            active=body.active,
            notes=body.notes,
            actor="Web",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if med is None:
        raise HTTPException(status_code=404, detail="Medicamento no encontrado")
    return _enrich_med(med, db, settings)


@router.delete("/api/medications/{med_id}")
async def delete_medication(med_id: int, request: Request) -> dict[str, bool]:
    db = _db(request)
    filenames = db.list_photo_filenames_for_med(med_id)
    if not db.delete_medication(med_id):
        raise HTTPException(status_code=404, detail="Medicamento no encontrado")
    photos_dir = get_photos_dir(db.db_path)
    for fn in filenames:
        delete_photo_file(photos_dir, fn)
    return {"deleted": True}


MAX_PHOTO_BYTES = 15 * 1024 * 1024


@router.post("/api/medications/{med_id}/photos", status_code=201)
async def upload_medication_photo(
    med_id: int,
    request: Request,
    file: UploadFile = File(...),
    kind: Literal["package", "dose", "other"] = Form("package"),
    caption: str = Form(""),
) -> dict[str, Any]:
    db = _db(request)
    if db.get_medication(med_id) is None:
        raise HTTPException(status_code=404, detail="Medicamento no encontrado")
    raw = await file.read(MAX_PHOTO_BYTES + 1)
    if len(raw) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="La imagen supera el máximo de 15 MB.")
    if not raw:
        raise HTTPException(status_code=400, detail="Archivo vacío.")
    photos_dir = get_photos_dir(db.db_path)
    try:
        filename, mimetype = process_and_save_photo(raw, photos_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = db.add_medication_photo(
        med_id=med_id,
        kind=kind,
        filename=filename,
        mimetype=mimetype,
        caption=caption,
    )
    return {
        "id": row["id"],
        "med_id": row["med_id"],
        "kind": row["kind"],
        "caption": row["caption"],
        "url": f"/api/photos/{row['id']}",
        "created_at": row["created_at"],
    }


@router.get("/api/photos/{photo_id}")
async def get_medication_photo(photo_id: int, request: Request) -> FileResponse:
    db = _db(request)
    row = db.get_medication_photo(photo_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    path = get_photos_dir(db.db_path) / row["filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Archivo de foto no encontrado")
    return FileResponse(
        path,
        media_type=row["mimetype"],
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.delete("/api/photos/{photo_id}")
async def delete_medication_photo(photo_id: int, request: Request) -> dict[str, bool]:
    db = _db(request)
    row = db.delete_medication_photo(photo_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    delete_photo_file(get_photos_dir(db.db_path), row["filename"])
    return {"deleted": True}


@router.post("/api/medications/{med_id}/purchase")
async def record_purchase(
    med_id: int, body: StockPurchaseRequest, request: Request
) -> dict[str, Any]:
    db = _db(request)
    settings = _settings(request)
    try:
        med = db.adjust_stock(med_id, delta=body.quantity, reason=body.reason, user=body.user)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _enrich_med(med, db, settings)


@router.post("/api/medications/{med_id}/set-stock")
async def set_stock(
    med_id: int, body: StockSetRequest, request: Request
) -> dict[str, Any]:
    db = _db(request)
    settings = _settings(request)
    try:
        med = db.set_stock(med_id, new_quantity=body.quantity, reason=body.reason, user=body.user)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _enrich_med(med, db, settings)


@router.get("/api/stock-movements")
async def list_stock_movements(
    request: Request,
    med_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return _db(request).list_stock_movements(med_id=med_id, limit=limit)


# ---- Dose Logs & Meals ----


@router.get("/api/dose-logs")
async def list_dose_logs(
    request: Request,
    limit: int = Query(default=30, ge=1, le=200),
) -> list[dict[str, Any]]:
    return _db(request).list_dose_logs(limit=limit)


@router.post("/api/meals/{meal}/trigger-reminder")
async def trigger_meal_reminder(
    meal: Literal["lunch", "dinner"],
    request: Request,
    body: MealActionRequest | None = None,
) -> dict[str, Any]:
    settings = _settings(request)
    date_str = (
        body.date
        if body and body.date
        else datetime.now(tz=settings.get_tz()).date().isoformat()
    )
    sent = await _bot(request).send_meal_reminder(date_str, meal, force=True)
    if not sent:
        raise HTTPException(
            status_code=400,
            detail="No se pudo enviar: configurá primero el grupo de WhatsApp en Config & Familia.",
        )
    return {"sent": True, "date": date_str, "meal": meal}


@router.post("/api/meals/{meal}/confirm")
async def confirm_meal_api(
    meal: Literal["lunch", "dinner"],
    request: Request,
    body: MealActionRequest | None = None,
) -> dict[str, Any]:
    settings = _settings(request)
    date_str = (
        body.date
        if body and body.date
        else datetime.now(tz=settings.get_tz()).date().isoformat()
    )
    given_by = body.given_by if body else "Web"
    return await _bot(request).confirm_meal(
        date_str=date_str,
        meal=meal,
        actor_jid=None,
        actor_name=given_by,
        from_dm=False,
    )


@router.post("/api/meals/{meal}/reset")
async def reset_meal_api(
    meal: Literal["lunch", "dinner"],
    request: Request,
    body: MealActionRequest | None = None,
) -> dict[str, Any]:
    settings = _settings(request)
    date_str = (
        body.date
        if body and body.date
        else datetime.now(tz=settings.get_tz()).date().isoformat()
    )
    deleted = _db(request).reset_meal_log(date_str, meal)
    return {"reset": deleted, "date": date_str, "meal": meal}


# ---- Appointments ----


@router.get("/api/appointments")
async def list_appointments(
    request: Request, upcoming_only: bool = False
) -> list[dict[str, Any]]:
    now_ts = int(time.time()) - 3600
    return _db(request).list_appointments(upcoming_only=upcoming_only, from_ts=now_ts)


@router.post("/api/appointments", status_code=201)
async def create_appointment(
    body: AppointmentCreate, request: Request
) -> dict[str, Any]:
    db = _db(request)
    appt = db.create_appointment(
        dog_id=body.dog_id,
        dog_name=body.dog_name,
        checkup_type=body.checkup_type,
        location=body.location,
        scheduled_at=body.scheduled_at,
        notes=body.notes,
    )
    pub_res = await _bot(request).publish_appointment(
        appt,
        publish_whatsapp=body.publish_to_whatsapp,
        publish_gcal=body.publish_to_gcal,
    )
    fresh = db.get_appointment(appt["id"]) or appt
    return {"appointment": fresh, "publish_result": pub_res}


@router.put("/api/appointments/{appt_id}")
async def update_appointment(
    appt_id: int, body: AppointmentUpdate, request: Request
) -> dict[str, Any]:
    fields = body.model_dump(exclude_none=True)
    updated = _db(request).update_appointment(appt_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    gcal = getattr(request.app.state, "gcal", None)
    if gcal and gcal.is_configured() and updated.get("google_calendar_event_id"):
        try:
            await gcal.update_event(
                updated["google_calendar_event_id"], updated, _settings(request).get_tz()
            )
        except Exception:
            pass
    return updated


@router.delete("/api/appointments/{appt_id}")
async def delete_appointment(appt_id: int, request: Request) -> dict[str, bool]:
    db = _db(request)
    existing = db.get_appointment(appt_id)
    if existing is None or not db.delete_appointment(appt_id):
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    gcal = getattr(request.app.state, "gcal", None)
    if gcal and gcal.is_configured() and existing.get("google_calendar_event_id"):
        try:
            await gcal.delete_event(existing["google_calendar_event_id"])
        except Exception:
            pass
    return {"deleted": True}


# ---- Family & Settings & WAHA ----


@router.get("/api/family")
async def list_family(request: Request) -> list[dict[str, Any]]:
    return _db(request).list_family_members()


@router.post("/api/family/sync")
async def sync_family(request: Request) -> dict[str, Any]:
    synced = await _bot(request).sync_group_participants()
    return {"synced": synced, "family": _db(request).list_family_members()}


@router.put("/api/family/{member_id}")
async def update_family_member(
    member_id: int, body: FamilyUpdate, request: Request
) -> dict[str, Any]:
    try:
        updated = _db(request).update_family_member_name(member_id, body.display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Integrante no encontrado")
    return updated


@router.get("/api/settings")
async def get_settings(request: Request) -> dict[str, str]:
    return _db(request).get_all_settings()


@router.put("/api/settings")
async def update_settings(body: SettingsUpdate, request: Request) -> dict[str, str]:
    updates = body.model_dump(exclude_none=True)
    return _db(request).update_settings(updates)


@router.get("/api/waha/groups")
async def waha_groups(request: Request, refresh: bool = False) -> dict[str, Any]:
    db = _db(request)
    waha = _waha(request)
    settings = _settings(request)
    current_group_id = db.get_setting("whatsapp_group_id", "")
    target_name = normalize_text(
        db.get_setting("whatsapp_group_name", settings.whatsapp_group_name)
    )
    auto_bound: str | None = None
    session_status = "UNKNOWN"
    try:
        info = await waha.get_session_info()
        session_status = str(info.get("status") or "UNKNOWN")
        groups = await waha.list_groups(refresh=refresh)
        if not current_group_id and target_name:
            for g in groups:
                if normalize_text(g.get("subject", "")) == target_name:
                    db.set_setting("whatsapp_group_id", g["id"])
                    auto_bound = g["id"]
                    current_group_id = g["id"]
                    break
        if current_group_id:
            try:
                await _bot(request).sync_group_participants(current_group_id)
            except Exception:
                pass
        return {
            "ok": True,
            "session_status": session_status,
            "whatsapp_group_id": current_group_id,
            "auto_bound_group_id": auto_bound,
            "groups": groups,
        }
    except Exception as exc:
        return {
            "ok": False,
            "session_status": session_status,
            "whatsapp_group_id": current_group_id,
            "auto_bound_group_id": None,
            "groups": [],
            "error": str(exc),
        }


@router.post("/api/waha/sync-webhook")
async def sync_waha_webhook(request: Request) -> dict[str, Any]:
    waha = _waha(request)
    settings = _settings(request)
    try:
        res = await waha.configure_session_webhook(settings.webhook_public_url)
        return {"ok": True, **res}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error configurando WAHA: {exc}") from exc
