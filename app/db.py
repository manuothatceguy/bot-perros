from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator

from app.config import Settings
from app.logic import extract_wa_msg_key


DEFAULT_DOGS = ("Pancho", "Tito")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS medications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dog_id INTEGER NOT NULL REFERENCES dogs(id) ON DELETE CASCADE,
    dog_name TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL CHECK(unit IN ('pills', 'ml', 'drops')),
    dose_lunch REAL NOT NULL DEFAULT 0,
    dose_dinner REAL NOT NULL DEFAULT 0,
    timing TEXT NOT NULL CHECK(timing IN ('fasting_before', 'with_food', 'after_food')),
    fasting_offset_min INTEGER NOT NULL DEFAULT 30,
    interval_days INTEGER NOT NULL DEFAULT 1,
    start_date TEXT NOT NULL,
    end_date TEXT,
    stock_quantity REAL NOT NULL DEFAULT 0,
    alert_days INTEGER NOT NULL DEFAULT 5,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dose_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    meal TEXT NOT NULL CHECK(meal IN ('lunch', 'dinner')),
    claimed_by TEXT,
    claimed_by_jid TEXT,
    claimed_at INTEGER,
    given_by TEXT,
    given_by_jid TEXT,
    fasting_given_at INTEGER,
    fasting_remind_at INTEGER,
    fasting_reminded INTEGER NOT NULL DEFAULT 0,
    given_at INTEGER,
    group_poll_msg_id TEXT,
    dm_poll_msg_id TEXT,
    reminder_sent_at INTEGER,
    followup_sent_at INTEGER,
    UNIQUE(date, meal)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    med_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    delta REAL NOT NULL,
    reason TEXT NOT NULL,
    user TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dog_id INTEGER REFERENCES dogs(id) ON DELETE SET NULL,
    dog_name TEXT NOT NULL,
    checkup_type TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    scheduled_at INTEGER NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    reminded_24h INTEGER NOT NULL DEFAULT 0,
    reminded_2h INTEGER NOT NULL DEFAULT 0,
    whatsapp_event_sent INTEGER NOT NULL DEFAULT 0,
    google_calendar_event_id TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS family_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jid TEXT NOT NULL UNIQUE,
    lid TEXT,
    display_name TEXT NOT NULL,
    custom_name INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_family_members_lid ON family_members(lid);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_events (
    event_key TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS medication_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    med_id INTEGER NOT NULL REFERENCES medications(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'package' CHECK(kind IN ('package', 'dose', 'other')),
    caption TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    mimetype TEXT NOT NULL DEFAULT 'image/jpeg',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_medication_photos_med_id ON medication_photos(med_id);
"""


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


class Database:
    """Thread-safe SQLite repository with atomic transaction helpers."""

    def __init__(self, db_path: str, settings: Settings | None = None) -> None:
        self.db_path = db_path
        self.settings = settings or Settings(db_path=db_path)
        self._lock = threading.RLock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=15.0,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            if db_path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA foreign_keys=ON;")
            self.conn.execute("PRAGMA busy_timeout=15000;")
        self.init_schema()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        """Close SQLite connection."""
        with self._lock:
            self.conn.close()

    def init_schema(self) -> None:
        """Create schema and seed initial dogs and app settings if absent."""
        now = int(time.time())
        with self._lock:
            self.conn.executescript(SCHEMA_SQL)
            appt_cols = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(appointments)").fetchall()
            }
            if "google_calendar_event_id" not in appt_cols:
                self.conn.execute(
                    "ALTER TABLE appointments ADD COLUMN google_calendar_event_id TEXT"
                )
            count = self.conn.execute("SELECT COUNT(*) FROM dogs").fetchone()[0]
            if count == 0:
                for dog_name in DEFAULT_DOGS:
                    self.conn.execute(
                        "INSERT INTO dogs (name, notes, active, created_at) VALUES (?, '', 1, ?)",
                        (dog_name, now),
                    )
            defaults = {
                "whatsapp_group_id": self.settings.whatsapp_group_id,
                "whatsapp_group_name": self.settings.whatsapp_group_name,
                "lunch_time": self.settings.lunch_time,
                "lunch_followup_time": self.settings.lunch_followup_time,
                "dinner_time": self.settings.dinner_time,
                "dinner_followup_time": self.settings.dinner_followup_time,
                "send_photos_in_dm": "always",
            }
            for key, value in defaults.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                    (key, value),
                )

    # ---- Settings ----

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
            return row["value"] if row is not None else default

    def get_all_settings(self) -> dict[str, str]:
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM app_settings").fetchall()
            return {row["key"]: row["value"] for row in rows}

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def update_settings(self, updates: dict[str, str]) -> dict[str, str]:
        with self._tx() as conn:
            for key, value in updates.items():
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        return self.get_all_settings()

    # ---- Dedup ----

    def mark_event_processed(self, event_key: str, now_ts: int | None = None) -> bool:
        """Return True if event_key was new and recorded, False if duplicate."""
        ts = now_ts if now_ts is not None else int(time.time())
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_events (event_key, created_at) VALUES (?, ?)",
                (event_key, ts),
            )
            return cur.rowcount == 1

    def prune_processed_events(self, older_than_ts: int) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM processed_events WHERE created_at < ?",
                (older_than_ts,),
            )
            return cur.rowcount

    # ---- Dogs ----

    def list_dogs(self, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM dogs"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY id ASC"
        with self._lock:
            rows = self.conn.execute(query).fetchall()
            return [dict(r) for r in rows]

    def get_dog(self, dog_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM dogs WHERE id = ?", (dog_id,)).fetchone()
            return _row_to_dict(row)

    def get_dog_by_name(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dogs WHERE name = ? COLLATE NOCASE",
                (name.strip(),),
            ).fetchone()
            return _row_to_dict(row)

    def create_dog(self, name: str, notes: str = "", active: bool = True) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("El nombre del perro no puede estar vacío.")
        now = int(time.time())
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO dogs (name, notes, active, created_at) VALUES (?, ?, ?, ?)",
                (clean_name, notes.strip(), 1 if active else 0, now),
            )
            dog_id = cur.lastrowid
        return self.get_dog(int(dog_id))  # type: ignore[return-value]

    def update_dog(
        self,
        dog_id: int,
        name: str | None = None,
        notes: str | None = None,
        active: bool | None = None,
    ) -> dict[str, Any] | None:
        with self._tx() as conn:
            existing = conn.execute("SELECT * FROM dogs WHERE id = ?", (dog_id,)).fetchone()
            if existing is None:
                return None
            new_name = existing["name"] if name is None else name.strip()
            if not new_name:
                raise ValueError("El nombre del perro no puede estar vacío.")
            new_notes = existing["notes"] if notes is None else notes.strip()
            new_active = existing["active"] if active is None else (1 if active else 0)
            conn.execute(
                "UPDATE dogs SET name = ?, notes = ?, active = ? WHERE id = ?",
                (new_name, new_notes, new_active, dog_id),
            )
            if new_name != existing["name"]:
                conn.execute(
                    "UPDATE medications SET dog_name = ? WHERE dog_id = ?",
                    (new_name, dog_id),
                )
                conn.execute(
                    "UPDATE appointments SET dog_name = ? WHERE dog_id = ?",
                    (new_name, dog_id),
                )
        return self.get_dog(dog_id)

    def delete_dog(self, dog_id: int) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM dogs WHERE id = ?", (dog_id,))
            return cur.rowcount > 0

    # ---- Medications ----

    def list_medications(
        self, active_only: bool = False, dog_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("active = 1")
        if dog_id is not None:
            clauses.append("dog_id = ?")
            params.append(dog_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM medications{where} ORDER BY dog_name COLLATE NOCASE ASC, name COLLATE NOCASE ASC",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_medication(self, med_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
            return _row_to_dict(row)

    def create_medication(
        self,
        *,
        dog_id: int | None = None,
        dog_name: str | None = None,
        name: str,
        unit: str,
        dose_lunch: float = 0.0,
        dose_dinner: float = 0.0,
        timing: str = "with_food",
        fasting_offset_min: int = 30,
        interval_days: int = 1,
        start_date: str,
        end_date: str | None = None,
        stock_quantity: float = 0.0,
        alert_days: int = 5,
        active: bool = True,
        notes: str = "",
        actor: str = "sistema",
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._tx() as conn:
            dog_row = None
            if dog_id is not None:
                dog_row = conn.execute("SELECT * FROM dogs WHERE id = ?", (dog_id,)).fetchone()
            elif dog_name:
                dog_row = conn.execute(
                    "SELECT * FROM dogs WHERE name = ? COLLATE NOCASE",
                    (dog_name.strip(),),
                ).fetchone()
                if dog_row is None:
                    cur = conn.execute(
                        "INSERT INTO dogs (name, notes, active, created_at) VALUES (?, '', 1, ?)",
                        (dog_name.strip(), now),
                    )
                    dog_row = conn.execute(
                        "SELECT * FROM dogs WHERE id = ?", (cur.lastrowid,)
                    ).fetchone()
            if dog_row is None:
                raise ValueError("Perro no encontrado.")

            qty = round(max(0.0, float(stock_quantity)), 4)
            cur = conn.execute(
                """
                INSERT INTO medications (
                    dog_id, dog_name, name, unit, dose_lunch, dose_dinner, timing,
                    fasting_offset_min, interval_days, start_date, end_date,
                    stock_quantity, alert_days, active, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dog_row["id"],
                    dog_row["name"],
                    name.strip(),
                    unit,
                    round(max(0.0, float(dose_lunch)), 4),
                    round(max(0.0, float(dose_dinner)), 4),
                    timing,
                    max(0, int(fasting_offset_min)),
                    max(1, int(interval_days)),
                    start_date,
                    end_date or None,
                    qty,
                    max(0, int(alert_days)),
                    1 if active else 0,
                    notes.strip(),
                    now,
                    now,
                ),
            )
            med_id = int(cur.lastrowid)
            if qty > 0:
                conn.execute(
                    "INSERT INTO stock_movements (med_id, delta, reason, user, created_at) VALUES (?, ?, ?, ?, ?)",
                    (med_id, qty, "initial_stock", actor, now),
                )
        return self.get_medication(med_id)  # type: ignore[return-value]

    def update_medication(
        self,
        med_id: int,
        *,
        dog_id: int | None = None,
        dog_name: str | None = None,
        name: str | None = None,
        unit: str | None = None,
        dose_lunch: float | None = None,
        dose_dinner: float | None = None,
        timing: str | None = None,
        fasting_offset_min: int | None = None,
        interval_days: int | None = None,
        start_date: str | None = None,
        end_date: str | None = ...,
        stock_quantity: float | None = None,
        alert_days: int | None = None,
        active: bool | None = None,
        notes: str | None = None,
        actor: str = "web",
    ) -> dict[str, Any] | None:
        now = int(time.time())
        with self._tx() as conn:
            existing = conn.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
            if existing is None:
                return None

            resolved_dog_id = existing["dog_id"]
            resolved_dog_name = existing["dog_name"]
            if dog_id is not None:
                drow = conn.execute("SELECT * FROM dogs WHERE id = ?", (dog_id,)).fetchone()
                if drow is None:
                    raise ValueError("Perro no encontrado.")
                resolved_dog_id = drow["id"]
                resolved_dog_name = drow["name"]
            elif dog_name is not None:
                drow = conn.execute(
                    "SELECT * FROM dogs WHERE name = ? COLLATE NOCASE",
                    (dog_name.strip(),),
                ).fetchone()
                if drow is None:
                    raise ValueError(f"Perro '{dog_name}' no encontrado.")
                resolved_dog_id = drow["id"]
                resolved_dog_name = drow["name"]

            new_stock = existing["stock_quantity"]
            if stock_quantity is not None:
                new_stock = round(max(0.0, float(stock_quantity)), 4)
                delta = round(new_stock - float(existing["stock_quantity"]), 4)
                if abs(delta) > 1e-9:
                    conn.execute(
                        "INSERT INTO stock_movements (med_id, delta, reason, user, created_at) VALUES (?, ?, ?, ?, ?)",
                        (med_id, delta, "manual_edit", actor, now),
                    )

            new_end_date = existing["end_date"] if end_date is ... else (end_date or None)
            conn.execute(
                """
                UPDATE medications SET
                    dog_id = ?, dog_name = ?, name = ?, unit = ?,
                    dose_lunch = ?, dose_dinner = ?, timing = ?,
                    fasting_offset_min = ?, interval_days = ?, start_date = ?,
                    end_date = ?, stock_quantity = ?, alert_days = ?,
                    active = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    resolved_dog_id,
                    resolved_dog_name,
                    existing["name"] if name is None else name.strip(),
                    existing["unit"] if unit is None else unit,
                    existing["dose_lunch"] if dose_lunch is None else round(max(0.0, float(dose_lunch)), 4),
                    existing["dose_dinner"] if dose_dinner is None else round(max(0.0, float(dose_dinner)), 4),
                    existing["timing"] if timing is None else timing,
                    existing["fasting_offset_min"] if fasting_offset_min is None else max(0, int(fasting_offset_min)),
                    existing["interval_days"] if interval_days is None else max(1, int(interval_days)),
                    existing["start_date"] if start_date is None else start_date,
                    new_end_date,
                    new_stock,
                    existing["alert_days"] if alert_days is None else max(0, int(alert_days)),
                    existing["active"] if active is None else (1 if active else 0),
                    existing["notes"] if notes is None else notes.strip(),
                    now,
                    med_id,
                ),
            )
        return self.get_medication(med_id)

    def delete_medication(self, med_id: int) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM medications WHERE id = ?", (med_id,))
            return cur.rowcount > 0

    def adjust_stock(
        self,
        med_id: int,
        delta: float,
        reason: str,
        user: str,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        ts = now_ts if now_ts is not None else int(time.time())
        with self._tx() as conn:
            med = conn.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
            if med is None:
                raise ValueError("Medicamento no encontrado.")
            old_qty = float(med["stock_quantity"])
            new_qty = round(max(0.0, old_qty + float(delta)), 4)
            actual_delta = round(new_qty - old_qty, 4)
            conn.execute(
                "UPDATE medications SET stock_quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, ts, med_id),
            )
            conn.execute(
                "INSERT INTO stock_movements (med_id, delta, reason, user, created_at) VALUES (?, ?, ?, ?, ?)",
                (med_id, actual_delta, reason, user, ts),
            )
        return self.get_medication(med_id)  # type: ignore[return-value]

    def set_stock(
        self,
        med_id: int,
        new_quantity: float,
        reason: str,
        user: str,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        ts = now_ts if now_ts is not None else int(time.time())
        with self._tx() as conn:
            med = conn.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
            if med is None:
                raise ValueError("Medicamento no encontrado.")
            old_qty = float(med["stock_quantity"])
            clamped = round(max(0.0, float(new_quantity)), 4)
            delta = round(clamped - old_qty, 4)
            conn.execute(
                "UPDATE medications SET stock_quantity = ?, updated_at = ? WHERE id = ?",
                (clamped, ts, med_id),
            )
            conn.execute(
                "INSERT INTO stock_movements (med_id, delta, reason, user, created_at) VALUES (?, ?, ?, ?, ?)",
                (med_id, delta, reason, user, ts),
            )
        return self.get_medication(med_id)  # type: ignore[return-value]

    def list_stock_movements(
        self, med_id: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT sm.*, m.name AS med_name, m.dog_name, m.unit "
            "FROM stock_movements sm "
            "JOIN medications m ON m.id = sm.med_id"
        )
        params: list[Any] = []
        if med_id is not None:
            query += " WHERE sm.med_id = ?"
            params.append(med_id)
        query += " ORDER BY sm.created_at DESC, sm.id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # ---- Dose Logs ----

    def get_dose_log(self, date_str: str, meal: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dose_logs WHERE date = ? AND meal = ?",
                (date_str, meal),
            ).fetchone()
            return _row_to_dict(row)

    def get_dose_logs_for_date(self, date_str: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dose_logs WHERE date = ?",
                (date_str,),
            ).fetchall()
            return {row["meal"]: dict(row) for row in rows}

    def list_dose_logs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dose_logs ORDER BY date DESC, meal DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def find_dose_log_by_poll_msg_id(self, msg_id: str) -> tuple[dict[str, Any], str] | None:
        """Match poll ID against group_poll_msg_id or dm_poll_msg_id (full or canonical key)."""
        if not msg_id:
            return None
        incoming_key = extract_wa_msg_key(msg_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dose_logs ORDER BY date DESC, id DESC LIMIT 20"
            ).fetchall()
            for row in rows:
                for col, kind in (("group_poll_msg_id", "group"), ("dm_poll_msg_id", "dm")):
                    stored = row[col] or ""
                    if not stored:
                        continue
                    if stored == msg_id or (
                        incoming_key and extract_wa_msg_key(stored) == incoming_key
                    ):
                        return dict(row), kind
        return None

    def find_active_claim_for_sender(
        self, date_str: str, jids: list[str]
    ) -> dict[str, Any] | None:
        clean_jids = {j for j in jids if j}
        if not clean_jids:
            return None
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dose_logs WHERE date = ? AND given_at IS NULL AND claimed_by_jid IS NOT NULL "
                "ORDER BY claimed_at DESC",
                (date_str,),
            ).fetchall()
            for row in rows:
                if row["claimed_by_jid"] in clean_jids:
                    return dict(row)
        return None

    def upsert_dose_log_fields(
        self, date_str: str, meal: str, **fields: Any
    ) -> dict[str, Any]:
        allowed_cols = {
            "claimed_by",
            "claimed_by_jid",
            "claimed_at",
            "given_by",
            "given_by_jid",
            "fasting_given_at",
            "fasting_remind_at",
            "fasting_reminded",
            "given_at",
            "group_poll_msg_id",
            "dm_poll_msg_id",
            "reminder_sent_at",
            "followup_sent_at",
        }
        clean = {k: v for k, v in fields.items() if k in allowed_cols}
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO dose_logs (date, meal) VALUES (?, ?)",
                (date_str, meal),
            )
            if clean:
                assignments = ", ".join(f"{col} = ?" for col in clean)
                values = list(clean.values()) + [date_str, meal]
                conn.execute(
                    f"UPDATE dose_logs SET {assignments} WHERE date = ? AND meal = ?",
                    values,
                )
        return self.get_dose_log(date_str, meal)  # type: ignore[return-value]

    def clear_claim(self, date_str: str, meal: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE dose_logs SET
                    claimed_by = NULL,
                    claimed_by_jid = NULL,
                    claimed_at = NULL,
                    fasting_given_at = NULL,
                    fasting_remind_at = NULL,
                    fasting_reminded = 0,
                    dm_poll_msg_id = NULL
                WHERE date = ? AND meal = ? AND given_at IS NULL
                """,
                (date_str, meal),
            )
        return self.get_dose_log(date_str, meal)

    def reset_meal_log(self, date_str: str, meal: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM dose_logs WHERE date = ? AND meal = ?",
                (date_str, meal),
            )
            return cur.rowcount > 0

    def claim_fasting_reminders_due(self, now_ts: int) -> list[dict[str, Any]]:
        """Atomically select due fasting reminders and mark fasting_reminded=1."""
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT * FROM dose_logs
                WHERE fasting_remind_at IS NOT NULL
                  AND fasting_remind_at <= ?
                  AND fasting_reminded = 0
                  AND given_at IS NULL
                ORDER BY fasting_remind_at ASC
                """,
                (now_ts,),
            ).fetchall()
            result = [dict(r) for r in rows]
            if result:
                ids = [r["id"] for r in result]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE dose_logs SET fasting_reminded = 1 WHERE id IN ({placeholders})",
                    ids,
                )
            return result

    def confirm_meal_atomic(
        self,
        date_str: str,
        meal: str,
        given_by: str,
        given_by_jid: str | None,
        now_ts: int,
        due_meds: list[dict[str, Any]],
    ) -> tuple[bool, dict[str, Any], list[dict[str, Any]]]:
        """Atomically confirm a meal once (UNIQUE(date, meal)) and deduct due meds stock."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO dose_logs (date, meal) VALUES (?, ?)",
                (date_str, meal),
            )
            existing = conn.execute(
                "SELECT * FROM dose_logs WHERE date = ? AND meal = ?",
                (date_str, meal),
            ).fetchone()
            if existing is not None and existing["given_at"] is not None:
                return True, dict(existing), []

            conn.execute(
                """
                UPDATE dose_logs SET
                    given_by = ?,
                    given_by_jid = ?,
                    given_at = ?,
                    fasting_reminded = 1
                WHERE date = ? AND meal = ?
                """,
                (given_by, given_by_jid, now_ts, date_str, meal),
            )

            updated_meds: list[dict[str, Any]] = []
            for med in due_meds:
                med_id = int(med["id"])
                dose = float(med["due_dose"])
                if dose <= 0:
                    continue
                row = conn.execute(
                    "SELECT * FROM medications WHERE id = ?",
                    (med_id,),
                ).fetchone()
                if row is None:
                    continue
                old_qty = float(row["stock_quantity"])
                new_qty = round(max(0.0, old_qty - dose), 4)
                actual_delta = round(new_qty - old_qty, 4)
                conn.execute(
                    "UPDATE medications SET stock_quantity = ?, updated_at = ? WHERE id = ?",
                    (new_qty, now_ts, med_id),
                )
                conn.execute(
                    "INSERT INTO stock_movements (med_id, delta, reason, user, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (med_id, actual_delta, f"dose:{date_str}:{meal}", given_by, now_ts),
                )
                fresh = conn.execute(
                    "SELECT * FROM medications WHERE id = ?",
                    (med_id,),
                ).fetchone()
                if fresh is not None:
                    updated_meds.append(dict(fresh))

            updated_log = conn.execute(
                "SELECT * FROM dose_logs WHERE date = ? AND meal = ?",
                (date_str, meal),
            ).fetchone()
            return False, dict(updated_log), updated_meds  # type: ignore[arg-type]

    # ---- Appointments ----

    def list_appointments(
        self, upcoming_only: bool = False, from_ts: int | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM appointments"
        params: list[Any] = []
        if upcoming_only:
            cutoff = from_ts if from_ts is not None else int(time.time()) - 3600
            query += " WHERE scheduled_at >= ?"
            params.append(cutoff)
        query += " ORDER BY scheduled_at ASC"
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_appointment(self, appt_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
            return _row_to_dict(row)

    def create_appointment(
        self,
        *,
        dog_name: str,
        checkup_type: str,
        scheduled_at: int,
        location: str = "",
        notes: str = "",
        dog_id: int | None = None,
        whatsapp_event_sent: int = 0,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._tx() as conn:
            resolved_dog_id = dog_id
            resolved_dog_name = dog_name.strip()
            if resolved_dog_id is not None:
                drow = conn.execute("SELECT * FROM dogs WHERE id = ?", (resolved_dog_id,)).fetchone()
                if drow is not None:
                    resolved_dog_name = drow["name"]
            else:
                drow = conn.execute(
                    "SELECT * FROM dogs WHERE name = ? COLLATE NOCASE",
                    (resolved_dog_name,),
                ).fetchone()
                if drow is not None:
                    resolved_dog_id = drow["id"]
                    resolved_dog_name = drow["name"]

            cur = conn.execute(
                """
                INSERT INTO appointments (
                    dog_id, dog_name, checkup_type, location, scheduled_at, notes,
                    reminded_24h, reminded_2h, whatsapp_event_sent, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    resolved_dog_id,
                    resolved_dog_name,
                    checkup_type.strip(),
                    location.strip(),
                    int(scheduled_at),
                    notes.strip(),
                    int(whatsapp_event_sent),
                    now,
                ),
            )
            appt_id = int(cur.lastrowid)
        return self.get_appointment(appt_id)  # type: ignore[return-value]

    def update_appointment(self, appt_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "dog_id",
            "dog_name",
            "checkup_type",
            "location",
            "scheduled_at",
            "notes",
            "reminded_24h",
            "reminded_2h",
            "whatsapp_event_sent",
            "google_calendar_event_id",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return self.get_appointment(appt_id)
        with self._tx() as conn:
            existing = conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
            if existing is None:
                return None
            assignments = ", ".join(f"{k} = ?" for k in clean)
            conn.execute(
                f"UPDATE appointments SET {assignments} WHERE id = ?",
                list(clean.values()) + [appt_id],
            )
        return self.get_appointment(appt_id)

    def delete_appointment(self, appt_id: int) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM appointments WHERE id = ?", (appt_id,))
            return cur.rowcount > 0

    def claim_appointment_reminders(
        self, now_ts: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Atomically claim due 24h and 2h appointment reminders."""
        due_24h: list[dict[str, Any]] = []
        due_2h: list[dict[str, Any]] = []
        with self._tx() as conn:
            rows_24h = conn.execute(
                """
                SELECT * FROM appointments
                WHERE reminded_24h = 0
                  AND (scheduled_at - ?) > 9000
                  AND (scheduled_at - ?) <= 86400
                ORDER BY scheduled_at ASC
                """,
                (now_ts, now_ts),
            ).fetchall()
            due_24h = [dict(r) for r in rows_24h]
            if due_24h:
                ids = [r["id"] for r in due_24h]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE appointments SET reminded_24h = 1 WHERE id IN ({placeholders})",
                    ids,
                )

            rows_2h = conn.execute(
                """
                SELECT * FROM appointments
                WHERE reminded_2h = 0
                  AND (scheduled_at - ?) > 0
                  AND (scheduled_at - ?) <= 7200
                ORDER BY scheduled_at ASC
                """,
                (now_ts, now_ts),
            ).fetchall()
            due_2h = [dict(r) for r in rows_2h]
            if due_2h:
                ids = [r["id"] for r in due_2h]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE appointments SET reminded_2h = 1 WHERE id IN ({placeholders})",
                    ids,
                )
        return due_24h, due_2h

    # ---- Family Members ----

    def list_family_members(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM family_members ORDER BY display_name COLLATE NOCASE ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_family_member_by_jid_or_lid(self, identifier: str) -> dict[str, Any] | None:
        if not identifier:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM family_members WHERE jid = ? OR lid = ? LIMIT 1",
                (identifier, identifier),
            ).fetchone()
            return _row_to_dict(row)

    def upsert_family_member(
        self,
        *,
        jid: str,
        lid: str | None = None,
        display_name: str | None = None,
        custom_name: bool = False,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        ts = now_ts if now_ts is not None else int(time.time())
        clean_jid = jid.strip()
        fallback_name = clean_jid.split("@", 1)[0]
        clean_name = (display_name or "").strip() or fallback_name
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM family_members WHERE jid = ?",
                (clean_jid,),
            ).fetchone()
            if existing is None and lid:
                existing = conn.execute(
                    "SELECT * FROM family_members WHERE lid = ?",
                    (lid,),
                ).fetchone()

            if existing is None:
                conn.execute(
                    "INSERT INTO family_members (jid, lid, display_name, custom_name, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (clean_jid, lid, clean_name, 1 if custom_name else 0, ts),
                )
            else:
                new_jid = clean_jid if clean_jid.endswith("@c.us") else existing["jid"]
                new_lid = lid or existing["lid"]
                if existing["custom_name"] and not custom_name:
                    final_name = existing["display_name"]
                    final_custom = 1
                else:
                    final_name = clean_name if display_name else existing["display_name"]
                    final_custom = 1 if (custom_name or existing["custom_name"]) else 0
                conn.execute(
                    "UPDATE family_members SET jid = ?, lid = ?, display_name = ?, custom_name = ?, updated_at = ? "
                    "WHERE id = ?",
                    (new_jid, new_lid, final_name, final_custom, ts, existing["id"]),
                )
            row = conn.execute(
                "SELECT * FROM family_members WHERE jid = ? OR (lid IS NOT NULL AND lid = ?) LIMIT 1",
                (clean_jid, lid or clean_jid),
            ).fetchone()
            return dict(row)  # type: ignore[arg-type]

    def update_family_member_name(
        self, member_id: int, display_name: str
    ) -> dict[str, Any] | None:
        clean_name = display_name.strip()
        if not clean_name:
            raise ValueError("El nombre no puede estar vacío.")
        now = int(time.time())
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE family_members SET display_name = ?, custom_name = 1, updated_at = ? WHERE id = ?",
                (clean_name, now, member_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM family_members WHERE id = ?", (member_id,)).fetchone()
            return _row_to_dict(row)

    # ---- Medication Photos ----

    def list_medication_photos(self, med_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM medication_photos"
        params: list[Any] = []
        if med_id is not None:
            query += " WHERE med_id = ?"
            params.append(med_id)
        query += " ORDER BY CASE kind WHEN 'package' THEN 0 WHEN 'dose' THEN 1 ELSE 2 END, id ASC"
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_medication_photo(self, photo_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM medication_photos WHERE id = ?",
                (photo_id,),
            ).fetchone()
            return _row_to_dict(row)

    def add_medication_photo(
        self,
        med_id: int,
        kind: str,
        filename: str,
        mimetype: str = "image/jpeg",
        caption: str = "",
    ) -> dict[str, Any]:
        if kind not in ("package", "dose", "other"):
            raise ValueError("Tipo de foto inválido.")
        now = int(time.time())
        with self._tx() as conn:
            med = conn.execute("SELECT id FROM medications WHERE id = ?", (med_id,)).fetchone()
            if med is None:
                raise ValueError("Medicamento no encontrado.")
            cur = conn.execute(
                """
                INSERT INTO medication_photos (med_id, kind, caption, filename, mimetype, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (med_id, kind, caption.strip(), filename, mimetype, now),
            )
            photo_id = int(cur.lastrowid)
        return self.get_medication_photo(photo_id)  # type: ignore[return-value]

    def delete_medication_photo(self, photo_id: int) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM medication_photos WHERE id = ?",
                (photo_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM medication_photos WHERE id = ?", (photo_id,))
            return dict(row)

    def list_photo_filenames_for_med(self, med_id: int) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT filename FROM medication_photos WHERE med_id = ?",
                (med_id,),
            ).fetchall()
            return [r["filename"] for r in rows]

    def list_photo_filenames_for_dog(self, dog_id: int) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT mp.filename
                FROM medication_photos mp
                JOIN medications m ON m.id = mp.med_id
                WHERE m.dog_id = ?
                """,
                (dog_id,),
            ).fetchall()
            return [r["filename"] for r in rows]
