from __future__ import annotations

from datetime import datetime
from io import BytesIO
import json

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from app.bot import DogBot
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.photos import build_reference_image, get_photos_dir, process_and_save_photo
from app.waha import WahaClient
from tests.conftest import FakeWahaClient


def _make_png_bytes(width: int = 800, height: int = 600, color=(200, 60, 40, 255)) -> bytes:
    buf = BytesIO()
    img = Image.new("RGBA", (width, height), color)
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_photo_resize_rgba_and_side_by_side_composite(tmp_path):
    photos_dir = tmp_path / "photos"
    fn1, mime1 = process_and_save_photo(_make_png_bytes(2400, 1200, (220, 80, 50, 255)), photos_dir)
    assert fn1.endswith(".jpg")
    assert mime1 == "image/jpeg"
    with Image.open(photos_dir / fn1) as im:
        assert max(im.size) == 1280
        assert im.mode == "RGB"

    fn2, _ = process_and_save_photo(_make_png_bytes(500, 500, (40, 160, 220, 255)), photos_dir)

    single = build_reference_image([{"filename": fn1, "kind": "package", "caption": "Caja"}], photos_dir)
    assert single is not None
    assert single[1] == fn1
    assert "Caja / Frasco (Caja)" in single[2]

    combo = build_reference_image(
        [
            {"filename": fn1, "kind": "package", "caption": ""},
            {"filename": fn2, "kind": "dose", "caption": "1/2 pastilla"},
        ],
        photos_dir,
    )
    assert combo is not None
    combo_bytes, combo_name, combo_note = combo
    assert combo_name == "referencia.jpg"
    assert "Izq: Caja / Frasco" in combo_note
    assert "Der: Pastilla / Dosis (1/2 pastilla)" in combo_note
    with Image.open(BytesIO(combo_bytes)) as cim:
        assert cim.width > cim.height

    with pytest.raises(ValueError):
        process_and_save_photo(b"not-an-image", photos_dir)


def test_rest_photo_upload_serve_delete_and_cascade(
    settings: Settings, db: Database, fake_waha: FakeWahaClient
):
    app = create_app(settings=settings, db=db, waha=fake_waha, enable_scheduler=False)  # type: ignore[arg-type]
    photos_dir = get_photos_dir(db.db_path)

    with TestClient(app) as client:
        dogs = client.get("/api/dogs").json()
        r_med = client.post(
            "/api/medications",
            json={
                "dog_id": dogs[0]["id"],
                "name": "Enalapril",
                "unit": "pills",
                "dose_lunch": 0.5,
                "dose_dinner": 0.0,
                "timing": "with_food",
                "start_date": "2026-11-01",
                "stock_quantity": 10.0,
            },
        )
        med_id = r_med.json()["id"]

        # Reject invalid image
        r_bad = client.post(
            f"/api/medications/{med_id}/photos",
            data={"kind": "package"},
            files={"file": ("bad.txt", b"hello", "text/plain")},
        )
        assert r_bad.status_code == 400

        # Upload package + dose photos
        r_p1 = client.post(
            f"/api/medications/{med_id}/photos",
            data={"kind": "package", "caption": "Caja violeta"},
            files={"file": ("caja.png", _make_png_bytes(400, 300), "image/png")},
        )
        assert r_p1.status_code == 201
        p1 = r_p1.json()
        assert p1["url"].startswith("/api/photos/")

        r_p2 = client.post(
            f"/api/medications/{med_id}/photos",
            data={"kind": "dose", "caption": "Media pastilla"},
            files={"file": ("dosis.png", _make_png_bytes(300, 300), "image/png")},
        )
        assert r_p2.status_code == 201
        p2 = r_p2.json()

        # Serve image bytes
        r_img = client.get(p1["url"])
        assert r_img.status_code == 200
        assert r_img.headers["content-type"].startswith("image/jpeg")

        # Dashboard includes photos on both medications and today_meals.lunch.due_meds
        dash = client.get("/api/dashboard").json()
        med_obj = next(m for m in dash["medications"] if m["id"] == med_id)
        assert [p["kind"] for p in med_obj["photos"]] == ["package", "dose"]

        # Delete first photo -> file unlinked from disk
        fn1 = db.get_medication_photo(p1["id"])["filename"]
        assert (photos_dir / fn1).is_file()
        r_del = client.delete(f"/api/photos/{p1['id']}")
        assert r_del.status_code == 200
        assert not (photos_dir / fn1).exists()

        # Delete medication -> second photo file unlinked via cascade cleanup
        fn2 = db.get_medication_photo(p2["id"])["filename"]
        assert (photos_dir / fn2).is_file()
        assert client.delete(f"/api/medications/{med_id}").status_code == 200
        assert not (photos_dir / fn2).exists()

        # MCP includes list_medication_photos tool
        r_tools = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert r_tools.status_code == 200
        tool_names = {t["name"] for t in r_tools.json()["result"]["tools"]}
        assert "list_medication_photos" in tool_names


@pytest.mark.asyncio
async def test_waha_send_image_and_bot_dm_and_fotos_command(
    db: Database, fake_waha: FakeWahaClient, bot: DogBot
):
    # Verify real WahaClient.send_image_base64 via MockTransport
    sent_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sendImage":
            sent_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(201, json={"id": "true_549111@c.us_IMG1"})
        return httpx.Response(404)

    real_client = WahaClient("http://waha:3000", "sess1")
    await real_client._client.aclose()
    real_client._client = httpx.AsyncClient(
        base_url="http://waha:3000", transport=httpx.MockTransport(handler)
    )
    try:
        mid = await real_client.send_image_base64(
            "549111@c.us", "ref.jpg", "image/jpeg", b"imgbytes", caption="Test"
        )
        assert mid == "true_549111@c.us_IMG1"
        assert real_client.is_sent_id("IMG1")
        assert sent_payloads[0]["file"]["mimetype"] == "image/jpeg"
    finally:
        await real_client.close()

    # Bot flow with medication photos
    today = datetime.now(tz=bot.tz).date().isoformat()
    med_pancho = db.create_medication(
        dog_name="Pancho",
        name="Omeprazol",
        unit="pills",
        dose_lunch=0.5,
        dose_dinner=0.0,
        timing="fasting_before",
        start_date=today,
        stock_quantity=10.0,
    )
    med_tito = db.create_medication(
        dog_name="Tito",
        name="Jarabe",
        unit="ml",
        dose_lunch=2.5,
        dose_dinner=0.0,
        timing="with_food",
        start_date=today,
        stock_quantity=50.0,
    )
    photos_dir = get_photos_dir(db.db_path)
    fn_pkg, _ = process_and_save_photo(_make_png_bytes(400, 300), photos_dir)
    fn_dose, _ = process_and_save_photo(_make_png_bytes(300, 300), photos_dir)
    fn_tito, _ = process_and_save_photo(_make_png_bytes(350, 450), photos_dir)
    db.add_medication_photo(med_pancho["id"], "package", fn_pkg, caption="Caja")
    db.add_medication_photo(med_pancho["id"], "dose", fn_dose, caption="1/2 pastilla")
    db.add_medication_photo(med_tito["id"], "package", fn_tito, caption="Frasco")

    # 1. send_photos_in_dm="always" sends composite image for Pancho and single for Tito in DM
    await bot.claim_meal(today, "lunch", "54911999111@c.us", "Flor")
    assert len(fake_waha.sent_images) == 2
    assert "Pancho — Omeprazol*: *1/2 pastilla*" in fake_waha.sent_images[0]["caption"]
    assert "Izq: Caja / Frasco" in fake_waha.sent_images[0]["caption"]
    assert "Tito — Jarabe*: *2.5 ml*" in fake_waha.sent_images[1]["caption"]
    # DM poll still sent AFTER images so buttons sit at the bottom
    assert fake_waha.sent_polls[-1]["chat_id"] == "54911999111@c.us"

    # 2. send_photos_in_dm="on_demand" suppresses automatic photos in DM and adds /fotos hint
    fake_waha.sent_images.clear()
    db.set_setting("send_photos_in_dm", "on_demand")
    await bot.claim_meal(today, "lunch", "54911999111@c.us", "Flor")
    assert len(fake_waha.sent_images) == 0
    assert "/fotos" in fake_waha.sent_texts[-2]["text"]

    # 3. /fotos pancho sends only Pancho's photo
    res = await bot.handle_webhook(
        {
            "event": "message.any",
            "payload": {
                "id": "cmd_fotos_1",
                "from": "54911999111@c.us",
                "notifyName": "Flor",
                "body": "/fotos pancho",
            },
        }
    )
    assert res["action"] == "fotos_query"
    assert len(fake_waha.sent_images) == 1
    assert "Pancho — Omeprazol" in fake_waha.sent_images[0]["caption"]
