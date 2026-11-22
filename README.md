# 🐾 Bot Familiar de Perros - Pancho y Tito

Bot self-hosted (Docker + FastAPI + SQLite + APScheduler) integrado con **WAHA** para coordinar desde el grupo familiar de WhatsApp (**Perros**) y mensajes privados:

- **Medicación diaria y periódica:** pastillas (con fracciones `0.25`, `0.5`, `1.5`), ml y gotas, con soporte de frecuencia cada `X` días (`interval_days` + `start_date`), fecha de fin (`end_date`) y relación con la comida (`fasting_before`, `with_food`, `after_food`).
- **Flujo 1-toque sin errores de tipeo:**
  - En los horarios configurados (`12:00` almuerzo / `20:00` cena — o `30 min` antes si hay medicación en ayunas), envía una **encuesta** al grupo.
  - Quien toca **✋ Yo les doy** recibe por **DM** el checklist paso a paso (Paso 1 ayunas / Paso 2 con la comida agrupado por perro) y una encuesta para avisar cuando dio lo de ayunas (dispara un timer privado persistido en SQLite) o confirmar que terminó.
  - Confirmación atómica con `UNIQUE(date, meal)` para evitar doble descuento, registra quién dio la toma y la hora, descuenta stock y publica confirmación + alertas de compra en el grupo.
- **Control de stock y auditoría:** cálculo automático de `días de stock` (respetando frecuencia y `end_date`), tabla `stock_movements` y comandos `/stock`, `/compre`, `/set`.
- **Turnos veterinarios:** carga por WhatsApp (`/turno`) o Web UI, intenta publicar Evento nativo vía `POST /api/{session}/events` y hace fallback automático a archivo `.ics` + mensaje formateado cuando el motor es `NOWEB`. Recordatorios automáticos 24h y 2h antes.
- **Extensibilidad:** API REST (`/api/...`), Web UI mobile-first (`http://perros.local`) y servidor **MCP** (`/mcp` Streamable HTTP y `stdio`).

## Comandos por WhatsApp

- **Interacción diaria (preferida):** encuestas con botones táctiles o reacciones (`✋`, `👍`, `✅`, `❌`) o respuestas cortas (`yo`, `ayuno`, `listo`).
- `/dar` — Recibir el checklist del turno actual por privado.
- `/fotos [perro o remedio]` — Ver fotos de referencia (caja/frasco y dosis).
- `/stock` — Ver stock y días restantes de cada medicamento activo.
- `/compre <remedio> <cantidad>` — Sumar stock (ej. `/compre pancho omeprazol 30`).
- `/set <remedio> <cantidad>` — Recalibrar stock manualmente.
- `/turno <perro> | <DD/MM HH:MM> | <tipo> | <lugar> | <notas>` — Agendar turno veterinario.
- `/turnos` — Listar próximos turnos.
- `/grupo` — Vincular el grupo actual en caso de cambiarlo.
- `/ayuda` — Resumen de comandos.

## Variables de entorno

Ver [`.env.example`](.env.example):
- `TZ=America/Argentina/Buenos_Aires`
- `WAHA_URL`, `WAHA_API_KEY`, `WAHA_SESSION`
- `WHATSAPP_GROUP_ID`, `WHATSAPP_GROUP_NAME=Perros`
- `WEBHOOK_PUBLIC_URL=http://bot-perros:8000/webhook`
- `LUNCH_TIME=12:00`, `LUNCH_FOLLOWUP_TIME=14:00`, `DINNER_TIME=20:00`, `DINNER_FOLLOWUP_TIME=22:00`
- `DB_PATH=/app/data/bot_perros.db`
- `APP_HOST=perros.local`
- `APP_PUBLIC_URL=http://perros.local`

## Desarrollo y tests locales

```bash
uv venv
uv pip install -e ".[dev]"
.venv/bin/pytest -v
```

## Deploy con Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
```

## Servidor MCP

- **Streamable HTTP:** `http://perros.local/mcp` (o `http://localhost:3010/mcp`)
- **Stdio:** `python -m app.mcp_server`
