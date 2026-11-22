from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.routing import Route

from app.api import router as api_router
from app.bot import DogBot
from app.config import Settings
from app.db import Database
from app.gcal import GoogleCalendarClient
from app.logic import normalize_text
from app.mcp_server import create_mcp_server
from app.scheduler import BotScheduler
from app.waha import WahaClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    waha: WahaClient | None = None,
    gcal: GoogleCalendarClient | None = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    """Create and wire the FastAPI application, MCP server, scheduler, and Web UI."""
    cfg = settings or Settings()
    database = db or Database(cfg.db_path, settings=cfg)
    owns_waha = waha is None
    waha_client = waha or WahaClient(
        base_url=cfg.waha_url,
        session=cfg.waha_session,
        api_key=cfg.waha_api_key,
    )
    owns_gcal = gcal is None
    gcal_client = gcal or GoogleCalendarClient(
        calendar_id=cfg.google_calendar_id,
        service_account_file=cfg.google_service_account_file,
    )
    bot = DogBot(db=database, waha=waha_client, settings=cfg, gcal=gcal_client)
    scheduler = BotScheduler(db=database, bot=bot, tz=cfg.get_tz())
    mcp = create_mcp_server(db=database, bot=bot, settings=cfg)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Best-effort auto-discovery of family WhatsApp group if not set yet
        if not database.get_setting("whatsapp_group_id", ""):
            expected = normalize_text(
                database.get_setting("whatsapp_group_name", cfg.whatsapp_group_name)
            )
            if expected:
                try:
                    groups = await waha_client.list_groups(refresh=False)
                    for g in groups:
                        if normalize_text(g.get("subject", "")) == expected:
                            database.set_setting("whatsapp_group_id", g["id"])
                            logger.info("Auto-discovered group '%s' -> %s", g["subject"], g["id"])
                            break
                except Exception as exc:
                    logger.info("Startup WAHA group discovery skipped: %s", exc)

        if database.get_setting("whatsapp_group_id", ""):
            try:
                await bot.sync_group_participants()
            except Exception as exc:
                logger.info("Startup group participant sync skipped: %s", exc)

        if enable_scheduler:
            scheduler.start()

        async with mcp.session_manager.run():
            yield

        if enable_scheduler:
            scheduler.shutdown()
        if owns_waha:
            await waha_client.close()
        if owns_gcal:
            await gcal_client.close()
        if db is None:
            database.close()

    app = FastAPI(
        title="Bot Perros — Pancho y Tito",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.db = database
    app.state.waha = waha_client
    app.state.gcal = gcal_client
    app.state.bot = bot
    app.state.scheduler = scheduler
    app.state.mcp = mcp

    app.include_router(api_router)
    mcp_endpoint = mcp_app.routes[0].endpoint  # type: ignore[attr-defined]
    app.router.routes.append(
        Route("/mcp", endpoint=mcp_endpoint, methods=["GET", "POST", "DELETE"])
    )
    app.router.routes.append(
        Route("/mcp/", endpoint=mcp_endpoint, methods=["GET", "POST", "DELETE"])
    )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def __getattr__(name: str):
    if name == "app":
        return create_app()
    raise AttributeError(name)
