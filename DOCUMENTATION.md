# DocIndex — Technical Documentation

Internal documentation for developers joining the project. It describes the
architecture, database schema, request flows, the AI agent, and the frontend.
For setup instructions see `README.md`.

## Overview

DocIndex is a personal, searchable file drive. Users upload documents and
images into folders; every file is indexed (text extraction, OCR, AI image
captions) into a full-text index, and an agentic AI assistant answers
questions by searching and reading files step by step.

The app is a classic Flask server-rendered application with a pjax-style SPA
layer on top: pages are rendered by Jinja templates, and a client-side router
swaps the main content container without full reloads. AI chat responses are
streamed to the browser as NDJSON.

## Tech stack

- **Backend**: Python 3.11+, Flask (app factory pattern), SQLAlchemy +
  Flask-Migrate (Alembic), Flask-Login, Flask-WTF (CSRF).
- **Database**: SQLite by default (`instance/docindex.sqlite`), WAL journal
  mode with a 30 s busy timeout. `DATABASE_URL` allows any SQLAlchemy URI.
- **Indexing**: pdfplumber (PDF, incl. tables), python-docx (DOCX),
  pytesseract + Pillow (image OCR), `markdown` (MD preview).
- **AI**: any OpenAI-compatible HTTP endpoint (Ollama, LM Studio, OpenAI,
  Anthropic, Moonshot, ...) via plain `requests`.
- **Frontend**: Jinja templates, Tailwind CSS + DaisyUI via CDN, vanilla JS
  (no build step). FontAwesome icons, highlight.js, PDF.js, marked.

## Project layout

```
pdo-app/
├── run.py                  # dev entry point: create_app() + app.run()
├── create_admin.py         # CLI: create a user/admin interactively
├── config.py               # Config / TestConfig classes, env var loading
├── requirements.txt
├── Dockerfile, docker-compose.yml
├── instance/               # SQLite DB (gitignored runtime data)
├── uploads/                # files/ and thumbnails/ on disk
├── migrations/             # Flask-Migrate / Alembic
├── app/
│   ├── __init__.py         # create_app() factory
│   ├── extensions.py       # db, migrate, login_manager, csrf
│   ├── models.py           # SQLAlchemy models (see Database schema)
│   ├── routes/
│   │   ├── auth.py         # /login /register /logout
│   │   ├── drive.py        # drive browser, upload, file & folder ops
│   │   ├── search.py       # /search page + /api/search instant search
│   │   ├── ai.py           # /ai/chat NDJSON stream + conversations API
│   │   └── settings.py     # /settings/ai connections + /settings/profile
│   ├── services/
│   │   ├── drive_service.py      # multi-drive support, current-drive session
│   │   ├── file_service.py       # storage on disk, checksums, thumbnails
│   │   ├── indexing_service.py   # text extraction + captions -> file_index
│   │   ├── search_service.py     # LIKE-based scoring + snippets
│   │   ├── ai_service.py         # OpenAI-compatible HTTP client, config
│   │   └── agent_service.py      # agentic tool-calling loop
│   ├── templates/          # Jinja: base.html + auth/ drive/ search/
│   │                       #   settings/ components/
│   └── static/
│       ├── css/style.css
│       └── js/             # spa.js, main.js, drive.js, ai_chat.js, ai_page.js
└── tests/                  # pytest suite
```

## Application factory and entry points

- `app/__init__.py:create_app(config_class=Config)` builds the app: loads
  config, creates `instance/`, `uploads/files/` and `uploads/thumbnails/`,
  initializes extensions, registers blueprints (`auth`, `drive`, `search`,
  `ai`, `settings`) and a context processor that injects `ai_enabled`,
  `app_name`, `current_drive` and `user_drives` into every template.
- For SQLite, a `connect` event listener sets `PRAGMA journal_mode=WAL` and
  `PRAGMA busy_timeout=30000` to avoid "database is locked" errors under
  concurrent requests (indexing threads + web requests).
- `run.py` starts the dev server on `0.0.0.0:$PORT` (default 5000, debug on).
- `create_admin.py [username] [email] [password]` creates an admin user
  (prompts for missing arguments).

## Configuration

`config.py` loads `.env` via python-dotenv. `SECRET_KEY` is mandatory — the
app refuses to start without it.

| Variable              | Default                                        | Purpose |
|-----------------------|------------------------------------------------|---------|
| `SECRET_KEY`          | — (required)                                   | Flask session/CSRF signing |
| `DATABASE_URL`        | `sqlite:///instance/docindex.sqlite`           | SQLAlchemy URI |
| `TESSERACT_LANGS`     | `eng+por`                                      | OCR languages for pytesseract |
| `AI_ENABLED`          | `false`                                        | Enable AI features from env config |
| `AI_BASE_URL`         | `http://localhost:11434/v1`                    | OpenAI-compatible base URL |
| `AI_API_KEY`          | empty                                          | Bearer token (empty for local servers) |
| `AI_MODEL`            | `llama3.1`                                     | Chat model for the assistant |
| `AI_VISION_MODEL`     | empty (falls back to `AI_MODEL`)               | Vision model for image captions |
| `AI_MAX_STEPS`        | `16`                                           | Global agent step limit (per-connection override available) |
| `AI_REQUEST_TIMEOUT`  | `300`                                          | HTTP timeout (s) for AI calls |
| `PORT`                | `5000`                                         | Dev server port (`run.py`) |

Non-env config constants: `MAX_CONTENT_LENGTH` 100 MB per request batch,
`MAX_FILE_SIZE` 16 MB per file, `ALLOWED_EXTENSIONS` (documents, code,
images incl. svg), `IMAGE_EXTENSIONS` (raster images, no svg),
`EDITABLE_EXTENSIONS` (text/code files editable in place), `INDEX_ASYNC`
(True in prod, False in tests).

`TestConfig` uses in-memory SQLite, disables CSRF and AI, sets
`INDEX_ASYNC=False` and redirects uploads to `instance/test_uploads`.

## Database schema

All models live in `app/models.py`. `utcnow()` provides timezone-aware UTC
defaults. Deleting a user cascades to everything they own; deleting a folder
cascades to its children and files; deleting a file cascades to its index row.

### `users`

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `username` | String(64), unique, indexed | |
| `email` | String(120), unique, indexed | |
| `password_hash` | String(255) | werkzeug `generate_password_hash` |
| `is_admin` | Boolean, default False | |
| `created_at` | DateTime | |

Relationships: `folders`, `files`, `drives`, `conversations`,
`ai_connections` (all `lazy="dynamic"`, cascade delete-orphan). Implements
Flask-Login's `UserMixin`; `set_password`/`check_password` helpers.

### `drives`

A named vault grouping a user's files and folders separately. The "current
drive" is stored in the Flask session (`session["drive_id"]`).

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `name` | String(120) | unique per user (enforced in service layer, case-insensitive) |
| `description` | String(500), default "" | |
| `user_id` | FK -> users.id, indexed | |
| `created_at` | DateTime | |

Relationships: `folders`, `files` (cascade delete-orphan).

### `folders`

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `name` | String(255) | |
| `parent_id` | FK -> folders.id, nullable | self-referential tree |
| `user_id` | FK -> users.id, indexed | |
| `drive_id` | FK -> drives.id, nullable, indexed | |
| `created_at` | DateTime | |

Relationships: `children` (self-ref, cascade delete-orphan), `files`.
`breadcrumb()` walks `parent` up to the root.

### `files` (`StoredFile`)

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `name` | String(255) | original display name |
| `stored_name` | String(255), unique | `<uuid4hex>.<ext>` on disk |
| `extension` | String(20), default "" | lowercase, no dot |
| `mime_type` | String(120), nullable | |
| `size` | Integer, default 0 | bytes |
| `checksum` | String(64), nullable | SHA-256; used for duplicate detection |
| `folder_id` | FK -> folders.id, nullable, indexed | NULL = drive root |
| `user_id` | FK -> users.id, indexed | |
| `drive_id` | FK -> drives.id, nullable, indexed | |
| `created_at`, `updated_at` | DateTime | `updated_at` auto-bumps |

Relationship: `index` (one-to-one `FileIndex`, cascade delete-orphan).
Properties: `is_image` (png/jpg/jpeg/gif/webp/bmp), `is_editable` (extension
in `EDITABLE_EXTENSIONS`).

### `file_index` (`FileIndex`)

Full-text search index for a stored file (one row per file).

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `file_id` | FK -> files.id, unique, indexed | |
| `extracted_text` | Text, nullable | PDF/DOCX/text/OCR content (max 500k chars) |
| `caption` | Text, nullable | AI-generated image caption |
| `word_count`, `line_count`, `char_count` | Integer, nullable | content statistics |
| `status` | String(20), default "pending" | pending / ok / error |
| `error` | Text, nullable | extraction error message (truncated to 1000 chars) |
| `indexed_at` | DateTime, nullable | |

### `chat_conversations`

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `user_id` | FK -> users.id, indexed | |
| `title` | String(255), default "New conversation" | first question, truncated to 80 chars |
| `created_at`, `updated_at` | DateTime | |

Relationship: `messages` (ordered by `created_at`, cascade delete-orphan).

### `chat_messages`

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `conversation_id` | FK -> chat_conversations.id, indexed | |
| `role` | String(20) | `user` / `assistant` / `step` / `thinking` |
| `content` | Text, default "" | |
| `model` | String(120), **nullable** | model that produced the message (assistant rows only); shown in the chat UI next to the timestamp |
| `created_at` | DateTime | shown as the message timestamp in the chat UI |

Only `user` and `assistant` messages are sent back to the model as history;
`thinking` and `step` rows exist purely for the conversation-history UI.

### `ai_connections` (`AIConnection`)

A user-defined OpenAI-compatible provider connection. At most one per user
has `is_active=True` (enforced in the settings routes).

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `user_id` | FK -> users.id, indexed | |
| `name` | String(120) | |
| `base_url` | String(500) | e.g. `http://localhost:11434/v1` |
| `api_key` | String(500), default "" | stored in plaintext |
| `model` | String(120) | chat model |
| `vision_model` | String(120), default "" | empty -> falls back to `model` |
| `max_steps` | Integer, **nullable** | per-connection agent step limit; NULL falls back to global `AI_MAX_STEPS` |
| `is_active` | Boolean, default False | |
| `created_at` | DateTime | |

### `settings` (`Setting`)

Instance-wide key/value store. Known keys:

| Key | Values | Effect |
|-----|--------|--------|
| `registration_enabled` | `"1"` / `"0"` (default `"1"`) | when off, `GET/POST /register` redirect to `/login` and the login page hides the register link; toggled by admins on the profile page |

Helpers on the model: `Setting.get(key, default)`, `Setting.get_bool(key, default)`,
`Setting.set(key, value)`.

## HTTP surface

- `auth.py`: `GET/POST /login`, `GET/POST /register` (blocked when the
  `registration_enabled` setting is off), `GET /logout`.
- `drive.py` (all `login_required`): `/` and `/folder/<id>` (drive browser,
  `view` = grid/list/tree stored in session), `/drives/create|select|edit`,
  `/upload` (multi-file, HTML redirect or JSON when `Accept: application/json`),
  `/file/<id>/download|raw|thumbnail|rename|move|delete|view|edit|reindex|info`
  (info is JSON), `/folder/create`, `/folder/<id>/delete` (recursive),
  `/selection/delete` and `/selection/move` (bulk ops, JSON; move guards
  against moving a folder into itself/descendants).
- `search.py`: `GET /search` (results page; `?mode=ai` renders the full-page
  AI chat), `GET /api/search` (instant-search JSON, limit 8).
- `ai.py`: see "AI chat streaming" below.
- `settings.py` (`/settings/ai`): list/add/edit/activate/delete connections,
  `POST /settings/ai/test` and `/settings/ai/<id>/test` (ping the provider's
  `/models`), `POST /settings/ai/models` (fetch model list for dropdowns).
  `PROVIDERS` holds presets for OpenAI, Claude, Kimi, LM Studio, Ollama,
  Custom.
- `settings.py` (`/settings/profile`): user profile page — account card,
  email update (`POST /settings/profile/email`), password change
  (`POST /settings/profile/password`, requires the current password),
  per-drive stats (files, folders, space used, indexed words), AI usage
  (conversations, messages, active connection) and, for admins only,
  `POST /settings/profile/registration` to toggle the
  `registration_enabled` setting.

Ownership is enforced everywhere by filtering on `user_id` (`_get_owned`
returns 404 for foreign objects).

## Request flows

### Upload -> indexing -> search

1. `POST /upload` (`drive.py`) validates extensions against
   `ALLOWED_EXTENSIONS` and calls `file_service.save_upload()`:
   stores the file as `uploads/files/<uuid>.<ext>`, enforces `MAX_FILE_SIZE`,
   computes the SHA-256 checksum, creates the `StoredFile` row and a
   `FileIndex(status="pending")` row, generates a 256 px PNG thumbnail for
   images, and reports duplicate content (same checksum) back to the UI.
2. Indexing is triggered per file: `index_file_async()` spawns a daemon
   thread when `INDEX_ASYNC` is true, otherwise runs inline (tests).
3. `indexing_service.index_file()` extracts text by extension: pdfplumber
   for PDFs (text + tables), python-docx for DOCX, raw read for editable
   text/code files, pytesseract OCR for images. All capped at
   `MAX_TEXT_CHARS = 500_000`. For images, OCR failure is tolerated if an AI
   caption can still be produced (and vice versa); when the owner's active AI
   connection is enabled, `ai_service.caption_image()` describes the image
   with the vision model. Word/line/char stats are computed from text or
   caption. Any failure lands in `status="error"` with the message.
4. `search_service.search_files()` splits the query into up to 8 terms,
   builds `ILIKE %term%` conditions over `files.name`,
   `file_index.extracted_text` and `file_index.caption`, then scores matches
   in Python: name hit +10, caption hit +5, text hit +2 plus occurrence count
   (capped). Snippets are ~120 chars of context around the first hit with
   `<mark>` highlighting. Results are scoped to the current drive and capped
   at 50 (8 for the instant-search endpoint).

### Editing

`/file/<id>/edit` rewrites editable text files on disk
(`update_file_content` bumps size/checksum) and re-triggers indexing.
`/file/<id>/reindex` re-indexes on demand.

### AI chat streaming

`POST /ai/chat` (`routes/ai.py`):

1. Guards: AI must be enabled for the user (`ai_service.is_enabled`), and the
   message or attachments must be non-empty.
2. The conversation is fetched or created (title = first 80 chars of the
   question). A `user` `ChatMessage` is committed immediately (attached file
   names are appended as a `📎` line for display).
3. History = last 20 `user`/`assistant` messages. If files are attached, a
   synthetic system message is prepended listing `[id] name` so the agent can
   `read_file` them directly without searching.
4. The response is `application/x-ndjson` streamed with
   `stream_with_context`. Each agent event becomes one JSON line:
   `{"type": "thinking"|"step"|"tool_result"|"answer"|"error", ...}`.
   Errors (including `AIError` and unexpected exceptions) are emitted as an
   `error` event rather than killing the stream silently.
5. After the stream ends, the route persists the run: each thinking chunk as
   a `role="thinking"` message, each step as `role="step"`
   (`"Label: detail → summary"`), and the final answer as `role="assistant"`.

`GET /ai/conversations` lists conversations (each with `updated_at` and the
`model` that wrote the latest assistant reply, for the history list);
`GET /ai/conversations/<id>` returns the full message list with `created_at`
and `model` per message (including thinking/step rows for the history view);
`POST /ai/conversations/<id>/delete` removes one.

## Services

- **drive_service** — multi-drive support. `get_current_drive()` resolves the
  current drive from the session, lazily creates the default "Personal"
  drive, and migrates pre-drives files/folders (`drive_id IS NULL`) into the
  first drive. `create_drive`/`select_drive`/`update_drive` manage drives and
  the session pointer.
- **file_service** — physical storage: path helpers, `save_upload`,
  thumbnails, SHA-256 checksums + duplicate detection
  (`find_duplicates`, `duplicate_checksums`), `delete_file` (disk + DB +
  thumbnail), `update_file_content`, `read_text_content`.
- **indexing_service** — extraction and indexing (see flow above).
- **search_service** — query parsing, scoring, snippet generation.
- **ai_service** — OpenAI-compatible client: `config_for(user)` resolves the
  active configuration (see below), `chat_completion()` POSTs to
  `/chat/completions` (raises `AIError` on any failure), `list_models()` /
  `test_connection()` hit `/models`, `caption_image()` sends a base64
  data-URL image to the vision model.
- **agent_service** — the agentic loop (next section).

## The AI agent

`agent_service.run_agent_events(user, history, drive=None)` is a generator
that runs the multi-step tool-calling loop and yields events as they happen:

- `("thinking", text)` — the model's intermediate reasoning (message content
  alongside tool calls, or a `reasoning_content`/`reasoning` field from
  Kimi/DeepSeek-style thinking models).
- `("step", {"label", "detail"})` — a tool call being made (human labels like
  "Searched files").
- `("tool_result", {"label", "summary"})` — a one-line summary of what the
  tool returned (`_summarize_result`).
- `("answer", text)` — always the last event.

If the step budget is exhausted, the loop does not end with a dead end: it
makes one final `chat_completion` call *without* tools, asking the model to
write the best final answer with the information already gathered (the
"forced finalization"). If that call fails or returns empty, the answer
falls back to asking the user to rephrase.

`run_agent()` is the non-streaming wrapper returning `(answer, steps)`.

### Tools

`TOOLS` defines four OpenAI function-calling tools; `TOOL_HANDLERS` maps
names to implementations. All tools are scoped to the calling user, and
search/list are additionally scoped to the current drive.

- `search_files(query: string)` — full-text search over filenames, extracted
  text and captions; returns up to 10 `{file_id, name, snippet}`.
- `read_file(file_id: int, start: int = 0, length: int = 20000)` — chunked
  reading: `length` is clamped to 50 000, and the result carries `start`,
  `returned_chars`, `total_chars` and `has_more` so the model can page
  through large files (`start + returned_chars`). Content comes from
  `extracted_text`/`caption`, falling back to reading editable text files
  from disk.
- `list_files(folder_id?: int)` — folders and files (up to 100) in a folder
  or the drive root.
- `get_file_info(file_id: int)` — metadata: name, extension, size, dates,
  index status.

### System prompt

`SYSTEM_PROMPT` instructs the model to work step by step, state one or two
sentences of reasoning before every tool call, start with a broad
`search_files` then `read_file` the best results, make a single tool call per
step, keep reading while `has_more=true`, cite sources as
`[filename](file://ID)`, answer in the user's language, and never invent
content. When scoped to a drive, a line is appended telling the model only
that drive's files are visible.

### The nudge mechanism

Smaller/local models sometimes narrate the next step ("Let me read the
file...", "Vou ler o ficheiro...") without emitting the tool call, which
looks like a final answer but is really intermediate reasoning. When a
response has no tool calls, the loop checks `_INTENT_RE` (a regex over
Portuguese/English/Spanish intent phrases such as "let me", "I will", "vou",
"preciso", "voy a", "next") against the reasoning text. On a match — up to
`_MAX_NUDGES = 4` times — it yields the text as `thinking`, appends the
`_NUDGE` user message ("You described what you plan to do next but did not
call any tool..."), and continues the loop instead of ending mid-task. Text
without intent phrases is accepted as the final answer.

### Max steps resolution

The step budget per run is resolved in this order:

1. `AIConnection.max_steps` of the user's active connection (if set), via
   `ai_service.config_for(user)`.
2. Global `AI_MAX_STEPS` env var (default 16).

The same fallback applies inside `config_for` itself (`conn.max_steps or
AI_MAX_STEPS`).

## Frontend architecture

All JS is vanilla, loaded via CDN/`<script>` tags — no bundler.

### SPA router (`spa.js`)

Pjax-style navigation for authenticated pages (everything inside
`#page-content-container`):

- Intercepts clicks on same-origin links (excluding downloads, `/logout`,
  `data-no-spa`, modifier clicks) and `POST` form submissions, fetches the
  page, and swaps only the content container with an enter/exit animation;
  header, docks and the chat panel stay alive.
- Keeps an in-memory page cache (max 10 URLs); mutations clear it. Exposes
  `window.spaNavigate(url)` and `window.spaInvalidate()`. `popstate` is
  handled; network failures fall back to full page loads.
- Inline `<script>` tags in swapped pages are re-executed by cloning them,
  wrapping inline code in an IIFE so `const`/`let` don't collide on repeat
  visits.
- `updatePageState()` syncs the document title, nav-dock active state, and
  the hidden `folder_id`/`parent_id` inputs of the upload/new-folder modals.
  Header fragments (`#drive-dropdown`, `#edit-drive-modal`) are refreshed
  from the fetched document. Server flash messages are extracted and shown as
  toasts. A thin progress bar (`#spa-progress`) signals navigation.

### `main.js`

Global search dock with debounced (250 ms) instant search against
`/api/search`, keyboard navigation in the dropdown, the Google-style hero
search animation on `/search`, the upload modal dropzone, and global Alt
shortcuts: Alt+S focus search, Alt+1 search page, Alt+2 drive, Alt+U upload,
Alt+N new folder, Alt+A toggle AI chat; holding Alt reveals shortcut badges.

### `drive.js`

Drive page interactions: Ctrl/Cmd+click multi-select, cut/paste clipboard
(sessionStorage), bulk delete/move via `/selection/*`, file info modal
(`/file/<id>/info`), rename, and drag of selected files into the AI chat
using a custom `application/x-docindex-files` dataTransfer payload.
`window.currentFile` tracks the file open in the viewer for `@here`.

### Chat UIs (`ai_chat.js`, `ai_page.js`)

Two near-identical clients: `ai_chat.js` is the floating dock panel
(toggle/center modes, used everywhere), `ai_page.js` is the full-page
assistant at `/search?mode=ai` (adds suggestion chips). Both:

- POST `{message, conversation_id, attachments}` to `/ai/chat` and consume
  the NDJSON stream with `ReadableStream` + `TextDecoder`, buffering partial
  lines.
- During a run, `thinking`/`step`/`tool_result` events accumulate inside one
  collapsible `<details class="ai-reasoning">` block that is open while the
  agent works and auto-collapses when the answer arrives. In the history
  view, `thinking` messages render as collapsed `ai-reasoning` blocks and
  `step` messages as check-mark lines.
- Render answers with `marked`; `[name](file://ID)` citations are rewritten
  to `/file/<ID>/view` links.
- Attachments: `@here` attaches `window.currentFile`, `#query` opens a
  mention autocomplete backed by `/api/search`, drag & drop accepts drive
  files (attach directly) and OS files (upload via `POST /upload` with
  `Accept: application/json`, then attach). Chips show pending attachments.
- Conversation history: list, load and delete via the `/ai/conversations`
  endpoints.

`base.html` wraps `window.fetch` to attach the `X-CSRFToken` header (from
`<meta name="csrf-token">`) to same-origin mutating requests, so the JSON API
endpoints pass Flask-WTF validation without CSRF exemptions.

### Templates

`base.html` (header, nav dock, search hero/dock markup, CDN assets) +
`auth/login|register.html`, `drive/drive|view|edit.html`,
`search/results.html` and `search/ai.html` (full-page chat),
`settings/ai.html` (connection CRUD with provider presets, test button, model
dropdowns), `components/ai_chat.html` (dock panel) and
`components/upload_modal.html` / `new_folder_modal.html`.

## AI connections and settings

Users manage their own OpenAI-compatible connections at `/settings/ai`. A
connection stores base URL, API key (kept when the field is left blank on
edit), chat model, vision model and an optional per-connection `max_steps`.
Exactly one connection is active at a time; the first connection created
becomes active automatically. Provider presets prefill known URLs/models.

Runtime resolution (`ai_service.config_for(user)`): if the user has an active
connection it wins entirely (`enabled=True`, its URL/key/models/max_steps,
with `vision_model` falling back to `model` and `max_steps` to
`AI_MAX_STEPS`); otherwise the env config is used (`AI_ENABLED` etc.). The
same config drives the chat agent and image captioning at indexing time.

## Migrations

Flask-Migrate/Alembic in `migrations/`. Apply with:

```bash
flask --app run.py db upgrade
```

Revision history: `068c55433ef8` baseline (users, folders, files, file_index,
chat tables) -> `3afab4cdb2a7` ai_connections -> `b7f2c1d94e05` drives ->
`c4e91a2b7f30` drive description -> `d8a3f5b16c42` file_index stats
(word/line/char counts) -> `e5f1a2c38d44` nullable `max_steps` on
ai_connections.

## Testing

pytest suite in `tests/`, ~45 tests across 7 modules:

- `conftest.py` fixtures: `app` (fresh app with `TestConfig`, `create_all` /
  `drop_all` around each test; the app context is deliberately not kept
  pushed while clients make requests), `client` (test client), `user`
  (creates "alice", yields her id), `auth_client` (logged-in client).
- Tests mock the network: agent and AI tests patch
  `app.services.ai_service.chat_completion` with canned message dicts (tool
  calls, reasoning fields, etc.), and settings tests patch
  `ai_service.requests.get` for `/models` responses. No real AI backend is
  needed.
- `TestConfig` sets `INDEX_ASYNC=False`, so indexing runs inline and tests
  can assert on `file_index` rows immediately.
- Coverage: auth, drive CRUD/upload/isolation, multi-drive behavior,
  indexing + search, file stats, AI settings (connection CRUD, env
  fallback, isolation), and the agent (multi-step runs, `read_file`
  chunking, thinking/tool_result events, attachments, reasoning field, the
  nudge mechanism).

Run with `pytest`.

## Docker

`Dockerfile`: `python:3.11-slim` + Tesseract (eng/por), installs
requirements, and on start runs `flask --app run.py db upgrade && python
run.py`. `docker-compose.yml` exposes port 5000, loads `.env`, and mounts two
named volumes: `docindex_data` (`/app/instance`, the SQLite DB) and
`docindex_uploads` (`/app/uploads`). Start with `docker compose up --build`;
create the admin user with `docker compose exec docindex python
create_admin.py`.
