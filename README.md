# Stockman Backend

Django 5.2 (LTS) + Django REST Framework API for the Stockman paper-trading app. The backend is the single source of truth for authorization, money math, and market data — the frontend never duplicates these rules.

## Features

- Session + CSRF authentication; RBAC + ABAC authorization via groups with schema-validated attribute policies.
- Audited virtual wallets with a balanced, append-only double-entry ledger. Funding only through audited top-up/withdrawal — no admin balance edits.
- Spot trading: market and limit orders, FIFO cost lots, per-currency portfolio with realized/unrealized P&L.
- Market data behind a provider adapter interface: Binance (public REST) and EODHD (EGX, gated by `EGX_ACCESS_VERIFIED`).
- Price and percent-change alert rules evaluated by a background worker.
- SSE event stream (`/api/v1/events/`) with `Last-Event-ID` resume and `resync`.
- Append-only audit trail with secret redaction.
- JSON envelope `{data, metadata, errors}`; money is decimal strings; pagination via `page`/`page_size`.
- Internationalized API messages (en/ar/de/es).

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
cp .env.example .env     # then fill in secrets (DJANGO_SECRET_KEY, EODHD_API_TOKEN, ...)
uv sync
uv run python manage.py migrate
uv run python manage.py bootstrap   # creates the default groups/roles
```

## Run

```bash
# API server
uv run uvicorn config.asgi:application --host 127.0.0.1 --port 8000

# Background worker (market data ingestion, order matching, alert evaluation)
uv run python manage.py run_worker
```

## Tests and checks

```bash
uv run pytest            # test suite
uv run ruff check .      # lint
uv run mypy .            # type check
```

## API

- Base path: `/api/v1/`
- OpenAPI schema: `/api/v1/schema/` · Swagger UI: `/api/v1/docs/` · health: `/api/v1/health/`
- A categorized Postman collection is in [`postman/`](postman/stockman.postman_collection.json).

## Configuration

All settings come from environment variables (see `.env.example`). Key ones:

- `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `SECURE_COOKIES`
- `DATABASE_PATH` (SQLite file)
- `BINANCE_BASE_URL`, `BINANCE_SYMBOLS`, `MARKET_POLL_SECONDS`, `QUOTE_MAX_AGE_SECONDS`, `MARKET_ORDER_TIMEOUT_SECONDS`
- `EODHD_API_TOKEN`, `EGX_ACCESS_VERIFIED`, `EGX_SYMBOLS`, `EODHD_DAILY_CALL_LIMIT`
- `SSE_POLL_SECONDS`, `SSE_HEARTBEAT_SECONDS`, `EVENT_RETENTION_DAYS`
