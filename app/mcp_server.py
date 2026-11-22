from __future__ import annotations

from datetime import datetime
import time
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.bot import DogBot
from app.config import Settings
from app.db import Database
from app.logic import (
    calculate_stock_status,
    get_due_meds,
    get_max_fasting_offset,
)
from app.waha import WahaClient


def create_mcp_server(
    db: Database,
    bot: DogBot | None = None,
    settings: Settings | None = None,
) -> FastMCP:
    """Create the FastMCP server connected to the shared SQLite database."""
    cfg = settings or db.settings
    tz = cfg.get_tz()

    mcp = FastMCP(
        "bot-perros",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    def _enrich_med(med: dict[str, Any]) -> dict[str, Any]:
        today = datetime.now(tz=tz).date()
        logs = db.get_dose_logs_for_date(today.isoformat())
        given_meals = {m for m, r in logs.items() if r.get("given_at")}
        item = dict(med)
        item["stock_status"] = calculate_stock_status(item, today, given_meals)
        base = cfg.app_public_url.rstrip("/")
        item["photos"] = [
            {
                "id": p["id"],
                "med_id": p["med_id"],
                "kind": p["kind"],
                "caption": p["caption"],
                "url": f"{base}/api/photos/{p['id']}",
            }
            for p in db.list_medication_photos(int(med["id"]))
        ]
        return item

    @mcp.tool()
    def get_bot_status() -> dict[str, Any]:
        """Get current bot configuration, dogs, and today's summary."""
        now = datetime.now(tz=tz)
        return {
            "today": now.date().isoformat(),
            "tz": cfg.tz,
            "settings": db.get_all_settings(),
            "dogs": db.list_dogs(active_only=False),
            "active_medications_count": len(db.list_medications(active_only=True)),
            "upcoming_appointments_count": len(
                db.list_appointments(upcoming_only=True, from_ts=int(now.timestamp()) - 3600)
            ),
        }

    @mcp.tool()
    def get_today_schedule(date_str: str | None = None) -> dict[str, Any]:
        """Get medications due today (or YYYY-MM-DD) for lunch and dinner and confirmation status."""
        now = datetime.now(tz=tz)
        target_date = now.date() if not date_str else datetime.strptime(date_str, "%Y-%m-%d").date()
        iso = target_date.isoformat()
        meds = db.list_medications(active_only=True)
        logs = db.get_dose_logs_for_date(iso)
        meals = {}
        for meal in ("lunch", "dinner"):
            due = get_due_meds(meds, target_date, meal)
            meals[meal] = {
                "due_meds": due,
                "fasting_offset_min": get_max_fasting_offset(due),
                "log": logs.get(meal),
            }
        return {"date": iso, "meals": meals}

    @mcp.tool()
    def list_dogs(active_only: bool = False) -> list[dict[str, Any]]:
        """List registered dogs (Pancho, Tito, etc.)."""
        return db.list_dogs(active_only=active_only)

    @mcp.tool()
    def list_medication_photos(med_id: int | None = None) -> list[dict[str, Any]]:
        """List reference photos (package/dose) attached to medications."""
        base = cfg.app_public_url.rstrip("/")
        return [
            {
                "id": p["id"],
                "med_id": p["med_id"],
                "kind": p["kind"],
                "caption": p["caption"],
                "url": f"{base}/api/photos/{p['id']}",
                "created_at": p["created_at"],
            }
            for p in db.list_medication_photos(med_id=med_id)
        ]

    @mcp.tool()
    def add_dog(name: str, notes: str = "") -> dict[str, Any]:
        """Register a new dog."""
        return db.create_dog(name=name, notes=notes, active=True)

    @mcp.tool()
    def list_medications(
        active_only: bool = True, dog_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List medications with calculated stock_days and purchase alert flags."""
        dog_id = None
        if dog_name:
            d = db.get_dog_by_name(dog_name)
            if d is None:
                return []
            dog_id = d["id"]
        return [_enrich_med(m) for m in db.list_medications(active_only=active_only, dog_id=dog_id)]

    @mcp.tool()
    def add_medication(
        dog_name: str,
        name: str,
        unit: Literal["pills", "ml", "drops"] = "pills",
        dose_lunch: float = 0.0,
        dose_dinner: float = 0.0,
        timing: Literal["fasting_before", "with_food", "after_food"] = "with_food",
        fasting_offset_min: int = 30,
        interval_days: int = 1,
        start_date: str | None = None,
        end_date: str | None = None,
        stock_quantity: float = 0.0,
        alert_days: int = 5,
        notes: str = "",
    ) -> dict[str, Any]:
        """Add a medication for a dog (creates the dog if not present)."""
        start = start_date or datetime.now(tz=tz).date().isoformat()
        med = db.create_medication(
            dog_name=dog_name,
            name=name,
            unit=unit,
            dose_lunch=dose_lunch,
            dose_dinner=dose_dinner,
            timing=timing,
            fasting_offset_min=fasting_offset_min,
            interval_days=interval_days,
            start_date=start,
            end_date=end_date,
            stock_quantity=stock_quantity,
            alert_days=alert_days,
            notes=notes,
            actor="MCP",
        )
        return _enrich_med(med)

    @mcp.tool()
    def update_medication(
        med_id: int,
        name: str | None = None,
        dose_lunch: float | None = None,
        dose_dinner: float | None = None,
        timing: Literal["fasting_before", "with_food", "after_food"] | None = None,
        fasting_offset_min: int | None = None,
        interval_days: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        alert_days: int | None = None,
        active: bool | None = None,
        notes: str | None = None,
    ) -> dict[str, Any] | None:
        """Update an existing medication by ID."""
        med = db.update_medication(
            med_id,
            name=name,
            dose_lunch=dose_lunch,
            dose_dinner=dose_dinner,
            timing=timing,
            fasting_offset_min=fasting_offset_min,
            interval_days=interval_days,
            start_date=start_date,
            end_date=... if end_date is None else (end_date or None),
            alert_days=alert_days,
            active=active,
            notes=notes,
            actor="MCP",
        )
        return _enrich_med(med) if med else None

    @mcp.tool()
    def record_purchase(
        med_id: int, quantity: float, user: str = "MCP"
    ) -> dict[str, Any]:
        """Add purchased units to a medication's stock and log the movement."""
        if quantity <= 0:
            raise ValueError("La cantidad debe ser mayor a 0.")
        med = db.adjust_stock(med_id, delta=quantity, reason="purchase_mcp", user=user)
        return _enrich_med(med)

    @mcp.tool()
    def set_medication_stock(
        med_id: int, quantity: float, user: str = "MCP"
    ) -> dict[str, Any]:
        """Manually recalibrate a medication's exact stock quantity."""
        if quantity < 0:
            raise ValueError("El stock no puede ser negativo.")
        med = db.set_stock(med_id, new_quantity=quantity, reason="manual_set_mcp", user=user)
        return _enrich_med(med)

    @mcp.tool()
    async def confirm_meal(
        meal: Literal["lunch", "dinner"],
        given_by: str = "MCP",
        date_str: str | None = None,
        notify_whatsapp: bool = True,
    ) -> dict[str, Any]:
        """Confirm lunch or dinner once (idempotent) and deduct due medication stock."""
        now = datetime.now(tz=tz)
        iso = date_str or now.date().isoformat()
        if bot is not None and notify_whatsapp:
            try:
                return await bot.confirm_meal(
                    date_str=iso, meal=meal, actor_jid=None, actor_name=given_by
                )
            except Exception:
                pass
        target_date = datetime.strptime(iso, "%Y-%m-%d").date()
        due_meds = get_due_meds(db.list_medications(active_only=True), target_date, meal)
        already, log_row, updated = db.confirm_meal_atomic(
            date_str=iso,
            meal=meal,
            given_by=given_by,
            given_by_jid=None,
            now_ts=int(time.time()),
            due_meds=due_meds,
        )
        return {"already_confirmed": already, "log": log_row, "updated_meds": updated}

    @mcp.tool()
    def list_stock_movements(
        med_id: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List recent stock movements audit log."""
        return db.list_stock_movements(med_id=med_id, limit=limit)

    @mcp.tool()
    def list_appointments(upcoming_only: bool = True) -> list[dict[str, Any]]:
        """List veterinary appointments."""
        return db.list_appointments(
            upcoming_only=upcoming_only, from_ts=int(time.time()) - 3600
        )

    @mcp.tool()
    async def create_appointment(
        dog_name: str,
        checkup_type: str,
        scheduled_iso: str,
        location: str = "",
        notes: str = "",
        publish_to_whatsapp: bool = True,
        publish_to_gcal: bool = True,
    ) -> dict[str, Any]:
        """Create a veterinary appointment (scheduled_iso e.g. '2026-11-28T16:30') and optionally publish to WhatsApp and Google Calendar."""
        dt = datetime.fromisoformat(scheduled_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        appt = db.create_appointment(
            dog_name=dog_name,
            checkup_type=checkup_type,
            scheduled_at=int(dt.timestamp()),
            location=location,
            notes=notes,
        )
        pub: dict[str, Any] = {"mode": "skipped", "message_id": "", "gcal": {"ok": False, "skipped": True}}
        if bot is not None and (publish_to_whatsapp or publish_to_gcal):
            try:
                pub = await bot.publish_appointment(
                    appt,
                    publish_whatsapp=publish_to_whatsapp,
                    publish_gcal=publish_to_gcal,
                )
            except Exception as exc:
                pub = {"mode": "error", "error": str(exc)}
        return {"appointment": db.get_appointment(appt["id"]) or appt, "publish_result": pub}

    return mcp


def main() -> None:
    """Run MCP server over stdio against the configured SQLite database."""
    settings = Settings()
    db = Database(settings.db_path, settings=settings)
    waha = WahaClient(settings.waha_url, settings.waha_session, settings.waha_api_key)
    bot = DogBot(db=db, waha=waha, settings=settings)
    mcp = create_mcp_server(db=db, bot=bot, settings=settings)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
