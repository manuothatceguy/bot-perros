from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.waha import WahaClient
from tests.conftest import FakeWahaClient


@pytest.mark.asyncio
async def test_waha_client_http_transport_webhook_sync_groups_lids_and_ics_fallback():
    calls: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        calls.append((request.method, path, body))

        if request.method == "GET" and path == "/api/sessions/sess1":
            return httpx.Response(
                200,
                json={
                    "name": "sess1",
                    "status": "WORKING",
                    "config": {
                        "webhooks": [{"url": "http://other:9000/hook", "events": ["message"]}],
                        "noweb": {"markOnline": True, "store": {"enabled": False}},
                    },
                },
            )
        if request.method == "PUT" and path == "/api/sessions/sess1":
            return httpx.Response(200, json={"name": "sess1", "config": body["config"]})
        if request.method == "GET" and path == "/api/sess1/groups":
            return httpx.Response(
                200,
                json={
                    "120363111@g.us": {"id": "120363111@g.us", "subject": "Perros"},
                    "120363222@g.us": {"id": "120363222@g.us", "subject": "Familia"},
                },
            )
        if request.method == "GET" and path.startswith("/api/sess1/lids/"):
            return httpx.Response(200, json={"lid": "123@lid", "pn": "54911223344@c.us"})
        if request.method == "POST" and path == "/api/sess1/events":
            return httpx.Response(422, json={"message": "NotImplementedByEngineError"})
        if request.method == "POST" and path == "/api/sendFile":
            return httpx.Response(201, json={"id": "true_120363111@g.us_FILE1"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = WahaClient("http://waha:3000", "sess1")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="http://waha:3000", transport=transport)

    try:
        res = await client.configure_session_webhook("http://bot-perros:8000/webhook")
        assert res["updated"] is True
        put_calls = [c for c in calls if c[0] == "PUT"]
        assert len(put_calls) == 1
        hooks = put_calls[0][2]["config"]["webhooks"]
        assert len(hooks) == 2  # preserved third-party + bot-perros
        assert put_calls[0][2]["config"]["noweb"]["store"]["enabled"] is True

        groups = await client.list_groups()
        assert [g["subject"] for g in groups] == ["Familia", "Perros"]

        pn = await client.resolve_lid("123@lid")
        assert pn == "54911223344@c.us"

        from zoneinfo import ZoneInfo
        pub = await client.publish_appointment_event(
            "120363111@g.us",
            {
                "id": 1,
                "dog_name": "Pancho",
                "checkup_type": "Control",
                "location": "Vet",
                "scheduled_at": 1795500000,
                "notes": "",
            },
            ZoneInfo("America/Argentina/Buenos_Aires"),
        )
        assert pub["mode"] == "ics"
        assert client.is_sent_id("FILE1")
        assert client.is_sent_id("true_120363111@g.us_FILE1_678698@lid")
        client.remember_sent_response({"key": {"id": "3EB0RAW"}})
        assert client.is_sent_id("true_120363111@g.us_3EB0RAW")
        assert client.is_sent_id("true_120363111@g.us_3EB0RAW_678698@lid")
    finally:
        await client.close()


def test_rest_api_and_mcp_http_endpoints(
    tmp_path, settings: Settings, db: Database, fake_waha: FakeWahaClient
):
    # Clear group id initially to test auto-bind in /api/waha/groups
    db.set_setting("whatsapp_group_id", "")
    app = create_app(settings=settings, db=db, waha=fake_waha, enable_scheduler=False)  # type: ignore[arg-type]

    with TestClient(app) as client:
        # Web UI & Health & seeded dogs Pancho & Tito
        r_index = client.get("/")
        assert r_index.status_code == 200
        assert "Pancho" in r_index.text and "Tito" in r_index.text

        r_health = client.get("/health")
        assert r_health.status_code == 200
        assert r_health.json()["status"] == "ok"

        r_dogs = client.get("/api/dogs")
        assert r_dogs.status_code == 200
        names = [d["name"] for d in r_dogs.json()]
        assert names == ["Pancho", "Tito"]

        # Startup auto-bound "Perros" already
        assert r_health.json()["group_id"] == "120363000000001@g.us"

        # Clearing setting and calling /api/waha/groups auto-binds "Perros" too
        db.set_setting("whatsapp_group_id", "")
        r_groups = client.get("/api/waha/groups")
        assert r_groups.status_code == 200
        assert r_groups.json()["auto_bound_group_id"] == "120363000000001@g.us"

        # Create medication via REST
        r_med = client.post(
            "/api/medications",
            json={
                "dog_id": r_dogs.json()[0]["id"],
                "name": "Enalapril",
                "unit": "pills",
                "dose_lunch": 0.5,
                "dose_dinner": 0.5,
                "timing": "with_food",
                "interval_days": 1,
                "start_date": "2026-11-01",
                "stock_quantity": 10.0,
                "alert_days": 3,
            },
        )
        assert r_med.status_code == 201
        med_id = r_med.json()["id"]
        assert r_med.json()["stock_status"]["days_stock"] == 10

        # Purchase & set-stock
        r_buy = client.post(f"/api/medications/{med_id}/purchase", json={"quantity": 5.0})
        assert r_buy.status_code == 200
        assert r_buy.json()["stock_quantity"] == 15.0

        # Confirm lunch via REST
        r_conf = client.post("/api/meals/lunch/confirm", json={"given_by": "Manu"})
        assert r_conf.status_code == 200
        assert r_conf.json()["already_confirmed"] is False

        # Dashboard reflects everything
        r_dash = client.get("/api/dashboard")
        assert r_dash.status_code == 200
        assert r_dash.json()["today_meals"]["lunch"]["log"]["given_by"] == "Manu"

        # MCP Streamable HTTP JSON-RPC: initialize -> tools/list -> tools/call
        mcp_headers = {"Accept": "application/json, text/event-stream"}
        r_init = client.post(
            "/mcp",
            headers=mcp_headers,
            follow_redirects=False,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            },
        )
        assert r_init.status_code == 200
        assert r_init.json()["result"]["serverInfo"]["name"] == "bot-perros"

        r_tools = client.post(
            "/mcp",
            headers=mcp_headers,
            follow_redirects=False,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert r_tools.status_code == 200
        tool_names = {t["name"] for t in r_tools.json()["result"]["tools"]}
        assert {"get_bot_status", "list_medications", "record_purchase", "create_appointment"} <= tool_names

        r_call = client.post(
            "/mcp",
            headers=mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_dogs", "arguments": {}},
            },
        )
        assert r_call.status_code == 200
        assert "Pancho" in json.dumps(r_call.json())
