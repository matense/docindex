# GitHub Copilot Instructions for DocIndex

## 1. Project Architecture & Overview
- **Framework**: Flask 3.1 (Application Factory Pattern — `create_app` in `app/__init__.py`)
- **Database**: SQLite (SQLAlchemy ORM + Flask-Migrate), default location `instance/docindex.sqlite`
- **Frontend**: Server-side rendered Jinja2 templates + vanilla JS SPA + Tailwind CSS + DaisyUI (via CDN)
- **Service Layer**: Business logic lives in `app/services/` (`drive_service`, `file_service`, `indexing_service`, `search_service`, `ai_service`, `agent_service`). Keep routes thin.
- **Blueprints**: Routes organized by domain in `app/routes/`: `auth`, `drive`, `search`, `ai`, `settings`.
- **AI**: Any OpenAI-compatible endpoint. Connections are stored per-user in the `AIConnection` model and managed in the AI Settings page; `.env` values are only defaults/fallback.

## 2. Critical Developer Workflows

### Setup & Run
- **Entry point**: `run.py` (debug/auto-reload only when `FLASK_DEBUG=true` in `.env`)
- **Run locally**: `python run.py`
- **Database**: `flask --app run.py db upgrade` (Flask-Migrate/Alembic migrations in `migrations/` — always use migrations for schema changes)
- **First user**: `python create_admin.py`
- **Docker**: `docker compose up --build -d`, then `docker compose exec docindex python create_admin.py`

### Testing
- **Framework**: `pytest`
- **Configuration**: `tests/conftest.py` uses `TestConfig` (in-memory SQLite, `INDEX_ASYNC=False`, CSRF off)
- **Run tests**: `pytest`

## 3. Code Conventions & Patterns

### Backend (Python/Flask)
- **Service pattern**: static methods in service classes for business logic; avoid complex logic in routes.
- **Auth**: Flask-Login; protect routes with `@login_required`; access user via `current_user`.
- **Config**: settings in `config.py` read from environment (`os.environ.get`), loaded from `.env` via python-dotenv. Never hardcode secrets.
- **Indexing**: files are indexed asynchronously in a background thread on upload (`INDEX_ASYNC`); text extraction via pdfplumber / python-docx / pytesseract; images get AI captions when a vision model is configured.
- **AI chat**: `routes/ai.py` streams NDJSON events (`thinking`, `step`, `tool_result`, `answer`, `error`); `agent_service` runs the tool-call loop (max steps per connection, `AI_MAX_STEPS` default). Conversation state is saved only when a run completes.

### Frontend (HTML/JS/CSS)
- **Styling**: Tailwind utility classes + DaisyUI components.
- **JavaScript**: vanilla ES6+ in `app/static/js/` (`spa.js` router, `drive.js`, `ai_chat.js` dock, `ai_page.js` full-page chat).
- **Templates**: extend `app/templates/base.html`.

## 4. Key Files & Directories
- `app/__init__.py`: app factory, blueprint registration, SQLite pragmas (WAL).
- `app/models.py`: `User`, `Drive`, `Folder`, `StoredFile`, `FileIndex`, `AIConnection`, `ChatConversation`, `ChatMessage`.
- `config.py`: all configuration.
- `DOCUMENTATION.md`: detailed internal technical documentation — keep it up to date when changing architecture.

## 5. Common Tasks
- **Adding a feature**: model change → new Alembic migration (`flask --app run.py db migrate -m "..."`) → service method → routes → templates/JS → tests.
- **Schema changes**: always via Flask-Migrate migrations, never by editing the DB directly.
