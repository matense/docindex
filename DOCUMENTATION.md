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
│   │   ├── hashtag_service.py    # hashtags: storage, AI gen, bulk drive jobs
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
| `AI_HASHTAG_MAX_WORDS` | `6`                                           | Max words per AI-generated hashtag (user tags are not limited) |
| `AI_STREAMING`        | `true`                                         | Token-by-token chat streaming (non-streaming fallback is automatic) |
| `SEARCH_FTS`          | `true`                                         | FTS5 full-text search with BM25 ranking (falls back to ILIKE) |
| `INDEX_WORKERS`       | `2`                                            | Background drainers for the persistent index queue |
| `ERROR_LOG_KEEP`      | `2000`                                         | Newest rows kept in the error log (`/settings/logs`) |
| `PORT`                | `5000`                                         | Dev server port (`run.py`) |

Non-env config constants: `MAX_CONTENT_LENGTH` 100 MB per request batch,
`MAX_FILE_SIZE` 16 MB per file, `ALLOWED_EXTENSIONS` (documents, code,
images incl. svg), `IMAGE_EXTENSIONS` (raster images, no svg),
`EDITABLE_EXTENSIONS` (text/code files editable in place), `INDEX_ASYNC`,
`SYNC_ASYNC` and `HASHTAG_ASYNC` (True in prod, False in tests).

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
| `source_path` | String(500), nullable | absolute local folder root — set = synced drive |
| `last_synced_at` | DateTime, nullable | last successful sync |
| `last_sync_stats` | Text, nullable | JSON stats of the last sync (added/updated/removed/skipped) |
| `captions_enabled` | Boolean, default true | synced drives: AI image captions on/off |
| `index_workers` | Integer, default 1 | synced drives: parallel indexing workers (1–8) |
| `created_at` | DateTime | |

Relationships: `folders`, `files` (cascade delete-orphan).
Property: `is_synced` (`source_path IS NOT NULL`). **Synced drives** mirror a
local folder and are strictly read-only: files are not copied (`StoredFile.
source_path` points at the real file and `file_service.file_path()` resolves
it), upload/rename/move/delete/edit/merge/folder ops are blocked (routes
abort 400 and `file_service` guards raise `ValueError`), no version history
is kept, and sync is on-demand via `sync_service.sync_drive`. CRITICAL:
never call `purge_file`/`delete_file` on a synced file — removals during
sync only delete DB rows; the real file belongs to the user.

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
| `checksum` | String(64), nullable | SHA-256; used for per-drive duplicate detection |
| `folder_id` | FK -> folders.id, nullable, indexed | NULL = drive root |
| `user_id` | FK -> users.id, indexed | |
| `drive_id` | FK -> drives.id, nullable, indexed | |
| `source_path` | String(500), nullable | absolute path of the real file (synced files) |
| `deleted_at` | DateTime, nullable, indexed | set = in the trash (soft-delete) |
| `created_at`, `updated_at` | DateTime | `updated_at` auto-bumps |

Relationship: `index` (one-to-one `FileIndex`, cascade delete-orphan) and
`versions` (`FileVersion`, ordered newest first, cascade delete-orphan).
Properties: `is_image` (png/jpg/jpeg/gif/webp/bmp), `is_editable` (extension
in `EDITABLE_EXTENSIONS`).

**Trash (soft-delete).** Deleting a file only sets `deleted_at`; the blob,
thumbnail and version history stay on disk. Trashed files are hidden from
drive listings, search, the AI agent, attachments and profile stats (every
active-file query filters `deleted_at IS NULL`). Restoring clears the column;
purging (`purge_file`) removes blobs and rows permanently. The profile page
has a Trash section with restore / delete-forever / empty-trash actions.
Deleting a folder trashes its files with `folder_id` reset to NULL, so a
restore lands at the drive root.

### `file_versions` (`FileVersion`)

A snapshot of a file's previous content. Snapshots are taken *before* any
overwrite (same-name re-upload, edit, AI merge accept, restore); the live
file is always the current version. Blobs live in `uploads/versions/`
(`VERSIONS_FOLDER`) and are removed from disk by `purge_file`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `file_id` | FK -> files.id, indexed | |
| `version` | Integer | per-file sequence starting at 1 |
| `stored_name` | String(255), unique | `<uuid4hex>.<ext>` under `uploads/versions/` |
| `size` | Integer | bytes |
| `checksum` | String(64), nullable | SHA-256 of that version |
| `source` | String(20) | `upload` / `edit` / `merge` / `restore` |
| `note` | String(255), default "" | e.g. "Merged with 'b.txt'" |
| `created_at` | DateTime | |

Same-name re-upload into the same folder of the same drive replaces the
existing file (snapshot first) instead of creating a duplicate row; identical
content is a no-op. Text versions support diff vs current and in-place
restore; binary versions are download-only. The AI merge flow
(`/file/<id>/merge/<other_id>`) lets the AI propose a merged text that the
user reviews (server-side `difflib` diff) before accepting — accepting saves
a `merge` version and optionally deletes the other file.

### `file_index` (`FileIndex`)

Full-text search index for a stored file (one row per file).

| Column | Type | Notes |
|--------|------|-------|
| `id` | Integer PK | |
| `file_id` | FK -> files.id, unique, indexed | |
| `extracted_text` | Text, nullable | PDF/DOCX/text/OCR content (max 500k chars) |
| `caption` | Text, nullable | AI-generated image caption |
| `hashtags` | Text, nullable | JSON array of tags (user- or AI-generated); searchable |
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
| `rate_limit_rpm` | Integer, **nullable** | per-connection requests/minute limit; NULL falls back to global `AI_RATE_LIMIT_RPM` (default 30), 0 = unlimited. Enforced in `ai_service._rate_slot` (in-memory sliding window per connection; background jobs pass `block=True` to wait for a slot instead of failing); provider HTTP 429s get a friendly `AIError` with `Retry-After` |
| `is_active` | Boolean, default False | |
| `created_at` | DateTime | |

### `settings` (`Setting`)

Instance-wide key/value store. Known keys:

| Key | Values | Effect |
|-----|--------|--------|
| `registration_enabled` | `"1"` / `"0"` (default `"1"`) | when off, `GET/POST /register` redirect to `/login` and the login page hides the register link; toggled by admins on the profile page |

Helpers on the model: `Setting.get(key, default)`, `Setting.get_bool(key, default)`,
`Setting.set(key, value)`.

### `error_logs` (`ErrorLog`)

Central application log (`log_service`): `level` (`error`/`warning`), `source`
(`http`, `ai_chat`, `indexing`, `sync`, ...), `message`, optional `detail`
(traceback), `path`, `user_id`, `created_at`. Populated by:
`log_service.install(app)` — a `DbLogHandler` on `app.logger` (captures every
`app.logger.warning()/exception()`, including background indexing/sync
threads) plus an `errorhandler(Exception)` for unhandled request exceptions
(re-raised in debug/testing so the interactive debugger and test propagation
keep working) — and explicit `log_event()` calls for AI provider failures
(chat, hashtags, captions). Logging never raises (a thread-local guard
prevents recursion) and prunes to the newest `ERROR_LOG_KEEP` rows. The
admin-only page `GET /settings/logs` (filter by level/source/text, paginated
50/page, `POST /settings/logs/clear`) is linked from a profile card.

## HTTP surface

- `auth.py`: `GET/POST /login`, `GET/POST /register` (blocked when the
  `registration_enabled` setting is off), `GET /logout`.
- `drive.py` (all `login_required`): `/` and `/folder/<id>` (drive browser,
  `view` = grid/list/tree stored in session), `/drives/create|select|edit`,
  `/upload` (multi-file, HTML redirect or JSON when `Accept: application/json`;
  same-name same-folder upload becomes a new version),
  `/file/<id>/download|raw|thumbnail|rename|move|delete|view|edit|reindex|info`
  (delete is a soft-delete into the trash; `POST /file/<id>/restore`,
  `POST /file/<id>/purge` and `POST /trash/empty` manage the trash),
  `/file/<id>/history/<vid>/download|diff` and `POST .../restore` (version
  history; diff/restore are text-only), `/file/<id>/merge/<other_id>` (merge
  review page; `POST .../ai` JSON proposal, `POST .../preview` JSON diff,
  `POST .../accept` saves the reviewed merge)
  (info is JSON), `/folder/create`, `/folder/<id>/delete` (recursive),
  `/selection/delete` and `/selection/move` (bulk ops, JSON; move guards
  against moving a folder into itself/descendants),
  `POST /drives/sync-create` (create a synced drive from a local folder path)
  and `POST /drives/<id>/sync` (background re-sync; `GET .../sync/status`,
  `POST .../sync/pause|resume|stop` for progress control, `GET /sync/active`
  for the floating widget, `POST /drives/<id>/remove` deletes the synced
  drive and its index — never the real folder). All write routes abort
  400 for synced drives/files (`_guard_writable`).
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
  (conversations, messages, active connection), the trash bin (restore /
  delete forever / empty trash) and, for admins only,
  `POST /settings/profile/registration` to toggle the
  `registration_enabled` setting.

Ownership is enforced everywhere by filtering on `user_id` (`_get_owned`
returns 404 for foreign objects; `_get_file` additionally hides trashed
files from the normal file routes).

## Request flows

### Upload -> indexing -> search

1. `POST /upload` (`drive.py`) validates extensions against
   `ALLOWED_EXTENSIONS`. If a file with the same (secured) name already
   exists in the same folder of the current drive, `replace_with_upload()`
   snapshots the old content as a `FileVersion(source="upload")` and
   overwrites in place (identical checksum = no-op). Otherwise
   `file_service.save_upload()`:
   stores the file as `uploads/files/<uuid>.<ext>`, enforces `MAX_FILE_SIZE`,
   computes the SHA-256 checksum, creates the `StoredFile` row and a
   `FileIndex(status="pending")` row, generates a 256 px PNG thumbnail for
   images, and reports duplicate content (same checksum, within the same
   drive) back to the UI.
2. Indexing is triggered per file through the **persistent index queue**
   (`index_jobs` table): `enqueue_index()` persists a `pending` job (deduped,
   skips missing/trashed files) and `ensure_workers(n)` spawns up to n
   drainer threads that claim jobs FIFO (process-level lock + SQLite write
   serialization) and run `index_file()`. Failures are retried up to 3
   attempts, then the job is `error` (and the file's index badge shows it).
   On startup `recover_interrupted()` requeues jobs a crash left `running`,
   purges finished jobs older than 24h and resumes draining — no indexing
   work is ever lost. The queue can be **paused/resumed** (global in-memory
   flag; pending jobs stay safely in the DB) from the profile's "Indexing
   queue" card, which polls `GET /settings/index-queue/status`
   (`POST .../pause|resume`). With `INDEX_ASYNC=False` (tests) indexing runs
   inline as before.
3. `indexing_service.index_file()` extracts text by extension: pdfplumber
   for PDFs (text + tables), python-docx for DOCX, raw read for editable
   text/code files, pytesseract OCR for images. All capped at
   `MAX_TEXT_CHARS = 500_000`. For images, OCR failure is tolerated if an AI
   caption can still be produced (and vice versa); when the owner's active AI
   connection is enabled, `ai_service.caption_image()` describes the image
   with the vision model. Captions can also be (re)generated manually from
   the file view: "Generate Caption" asks the vision model via
   `POST /file/<id>/caption/suggest` (nothing is persisted), the user
   reviews/edits the text in a popup, and only accepting saves it via
   `POST /file/<id>/caption` (which refreshes the FTS row). Word/line/char
   stats are computed from text or
   caption. Any failure lands in `status="error"` with the message. After
   each successful commit, `search_service.fts_upsert()` refreshes the FTS5
   row.
4. `search_service.search_files()` uses the **FTS5** virtual table
   `file_fts` (rowid = `files.id`; columns: name, text, caption, tags) when
   available: up to 8 terms become a `"term"* AND ...` prefix MATCH, ranked
   by `bm25(file_fts, 0.2, 1.0, 0.6, 0.4)` (name > tags > caption > text,
   preserving the old boost order). The Python scoring still dominates the
   final sort; BM25 breaks ties. If FTS5 is missing (`SEARCH_FTS=false` or
   an old SQLite build) or the MATCH is invalid, it falls back to the
   original `ILIKE %term%` scan over `files.name`, `file_index.*` with
   Python scoring (name +10, hashtag +7, caption +5, text +2/occurrence).
   FTS matches by token/prefix rather than arbitrary substring: `insta`
   finds "installation", but mid-token substrings no longer match.
   Snippets are ~120 chars of context around the first hit with `<mark>`
   highlighting. All user text is HTML-escaped first and the result
   is returned as a `markupsafe.Markup` object, so templates render the
   highlight without double-escaping and without XSS risk. Each result also
   carries `name_html` (the escaped filename with `<mark>` highlights),
   `matches` (which of `name`/`tags`/`caption`/`content` matched, shown as
   badges) and `tags` (the file's hashtags, shown as chips).
   Results are scoped to the current drive and capped at 50 (8 for the
   instant-search endpoint, which also returns `name_html`).

   The FTS table is derived data — the source of truth is `file_index`. It
   is maintained by `fts_upsert`/`fts_delete` hooks (indexing, rename,
   hashtag changes, trash/restore/purge, synced-drive removal) and can be
   rebuilt anytime with `flask --app run.py reindex-fts`.

### Hashtags

Files can carry searchable hashtags (`file_index.hashtags`, a JSON array).
They are NEVER generated automatically at indexing time — only on explicit
user request:

- **Manual**: the file view has a chip editor (`hashtags.js`) that
  reads/writes `GET/POST /file/<id>/hashtags`. User tags have no word limit.
- **Per file with AI**: the "Create Hashtags" button opens a small review
  popup next to the button — `POST /file/<id>/hashtags/suggest` asks the AI
  (`hashtag_service.suggest_tags`, nothing is persisted), the suggestions
  are shown as chips the user can trim, and "Add to file" merges them into
  the file's tags (union, deduped server-side). The agent's `set_hashtags`
  tool remains available when the user asks for tags directly in the AI
  chat.
- **Per drive (bulk)**: the drive view's "Hashtags" button starts a
  background job in `hashtag_service` (`POST /drives/<id>/hashtags/start`
  with `workers` 1–8 and `overwrite`; `.../hashtags/status`,
  `.../pause|resume|stop`, `GET /hashtags/active` for the floating widget —
  `sync_widget.js` renders a second panel for it). Files that already have
  tags are skipped unless `overwrite` is set.

`hashtag_service` normalizes tags (trim, strip `#`, lowercase, dedupe, max
20 per file); AI-generated tags are limited to `AI_HASHTAG_MAX_WORDS` words
each (default 6). Bulk workers self-throttle through the AI connection's
per-minute rate limit (`ai_service._rate_slot` with `block=True`, sharing
the sliding window with interactive chat) and retry provider HTTP 429s up to
3 times per file before counting it as failed. Removing a synced drive also
cancels any running tagging job on it.

The AI agent is hashtag-aware: `search_files` results include each file's
tags, and the `list_hashtags` tool (`get_all_tags`) lets the model discover
the existing tag vocabulary and search with exact tag terms (see "Tools").

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
   `{"type": "thinking"|"thinking_token"|"answer_token"|"step"|"tool_result"|"answer"|"error", ...}`.
   Errors (including `AIError` and unexpected exceptions) are emitted as an
   `error` event rather than killing the stream silently.

   **Token streaming.** With `AI_STREAMING=true` (default), the agent calls
   `ai_service.chat_completion_stream()` — an SSE client (`stream=True` on
   the provider request) that yields `("token"|"reasoning", text)` deltas and
   a final `("done", message)` with the assembled message (tool_calls merged
   from deltas by index). A stream that dies mid-way keeps its partial
   content; one that fails before any delta triggers an automatic one-time
   fallback to the non-streaming `chat_completion()` (providers that reject
   streamed tool calls), so behaviour degrades gracefully — set
   `AI_STREAMING=false` to skip streaming entirely.
   The route forwards deltas as `thinking_token` / `answer_token` events.
   Because a model can stream content and *then* emit a tool call, the full
   `thinking` event that follows `answer_token` deltas carries
   `migrate: true`: the client moves its tentative answer bubble into the
   reasoning box. A `thinking` event whose text already arrived as
   `thinking_token` deltas is persisted but not re-sent (it would
   duplicate). The final `answer` event always carries the complete text and
   the client replaces the tentative bubble with the full markdown render.
5. After the stream ends, the route persists the run: each thinking chunk as
   a `role="thinking"` message, each step as `role="step"`
   (`"Label: detail → summary"`), and the final answer as `role="assistant"`.
   Token deltas are not persisted — only the assembled blocks, so the DB
   layout is identical with or without streaming.

   **Stop button.** While a request is in flight, the send button (both the
   chat widget and the AI page) becomes a red Stop button backed by an
   `AbortController`: clicking it aborts the fetch, the client keeps any
   partial text already rendered and adds a "Stopped by you" note. Closing
   the connection makes the next NDJSON write fail, which closes the
   server-side generator and ends the agent loop; nothing of the aborted
   run is persisted.

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
- **sync_service** — synced (read-only) drives. `create_synced_drive(user,
  path)` validates the local folder (exists, is dir, readable, not already
  synced) and creates a drive named after it; `sync_drive(drive)` walks the
  folder (`os.walk`, no symlink following), upserts `Folder`/`StoredFile`
  rows keyed by relative path, checksums content (SHA-256), re-indexes new
  and changed files, and DB-row-deletes vanished ones (never touching the
  real files). Skips disallowed extensions and oversize files. Syncs run in a
  **background thread** (`start_sync`; disable with `SYNC_ASYNC=False` — used
  by tests): a fast pre-pass counts files for the percentage, progress is
  tracked in memory (`get_status`, `get_active_job`) and the user can
  **pause/resume/stop** (`pause_sync`/`resume_sync`/`cancel_sync` — the
  worker aborts at the next file) — a floating widget polls
  `GET /sync/active` and drives `POST /drives/<id>/sync/pause|resume|stop`;
  `GET /drives/<id>/sync/status` returns the JSON state. Each finished sync
  stores its stats on `Drive.last_sync_stats` (JSON, shown on the profile
  page). `remove_synced_drive(drive)` stops any running sync, then deletes
  the drive row (files, folders, index and versions cascade) plus generated
  thumbnails — the real folder on disk is never touched. Rows are committed in batches of 50 so the UI stays responsive, and
  index jobs are enqueued in those same batches (persistent `index_jobs`
  queue) while the scan is still running — indexing overlaps the scan and the
  profile's "Indexing queue" card shows live progress during a sync. Workers
  come from a configurable pool of background drainers
  (`Drive.index_workers`, 1–8, default 1) sharing one queue — a thread per
  file would exhaust the connection pool. AI image captions can be toggled
  per synced drive (`Drive.captions_enabled`), both at creation and via
  `POST /drives/<id>/sync-settings` on the profile page. For
  file-based SQLite the engine uses `NullPool` + a 30 s busy timeout
  (`config.py`), so background threads never hit the QueuePool limit;
  in-memory test databases keep the default pool (NullPool would give each
  connection its own empty DB).
- **file_service** — physical storage: path helpers (`file_path` returns the
  real `source_path` for synced files), `save_upload`,
  thumbnails, SHA-256 checksums + per-drive duplicate detection
  (`find_duplicates`, `duplicate_checksums`), version history
  (`snapshot_version`, `replace_with_upload`, `restore_version`,
  `find_by_name`), trash (`delete_file` soft-deletes, `restore_file`,
  `purge_file` removes disk + DB + thumbnail + version blobs,
  `trashed_files`),
  `update_file_content` (snapshots before overwriting), `read_text_content`.
- **indexing_service** — extraction and indexing (see flow above).
- **search_service** — query parsing, scoring, snippet generation.
- **hashtag_service** — hashtags: normalization/storage (`set_tags`,
  `get_tags`), AI generation per file (`generate_tags`) and per-drive bulk
  background jobs (`start_job`, `pause_job`, `resume_job`, `cancel_job`,
  `get_active_job`) mirroring the sync job pattern.
- **ai_service** — OpenAI-compatible client: `config_for(user)` resolves the
  active configuration (see below), `chat_completion()` POSTs to
  `/chat/completions` (raises `AIError` on any failure),
  `chat_completion_stream()` is the SSE streaming variant used by the agent
  (see "AI chat streaming"), `list_models()` /
  `test_connection()` hit `/models`, `caption_image()` sends a base64
  data-URL image to the vision model.
- **agent_service** — the agentic loop (next section).

## The AI agent

`agent_service.run_agent_events(user, history, drive=None)` is a generator
that runs the multi-step tool-calling loop and yields events as they happen:

- `("thinking_token", text)` / `("answer_token", text)` — live token deltas
  (only when `AI_STREAMING` is on; see "AI chat streaming"). Model calls go
  through the internal `_complete()` helper, which streams when enabled and
  falls back once to non-streaming if the provider fails before any delta.
- `("thinking", text)` — the model's intermediate reasoning (message content
  alongside tool calls, or a `reasoning_content`/`reasoning` field from
  Kimi/DeepSeek-style thinking models).
- `("step", {"label", "detail"})` — a tool call being made (human labels like
  "Searched files").
- `("tool_result", {"label", "summary"})` — a one-line summary of what the
  tool returned (`_summarize_result`).
- `("answer", text)` — always the last event.

If the step budget is exhausted, the loop does not end with a dead end: it
makes one final completion call *without* tools (streamed like the rest),
asking the model to write the best final answer with the information already
gathered (the "forced finalization"). If that call fails or returns empty,
the answer falls back to asking the user to rephrase.

`run_agent()` is the non-streaming wrapper returning `(answer, steps)`
(token deltas are ignored; the final `answer` event carries the full text).

### Tools

`TOOLS` defines six OpenAI function-calling tools; `TOOL_HANDLERS` maps
names to implementations. All tools are scoped to the calling user, and
search/list are additionally scoped to the current drive.

- `search_files(query: string)` — full-text search over filenames, extracted
  text, hashtags and captions; returns up to 10 `{file_id, name, snippet,
  hashtags}`.
- `read_file(file_id: int, start: int = 0, length: int = 20000)` — chunked
  reading: `length` is clamped to 50 000, and the result carries `start`,
  `returned_chars`, `total_chars` and `has_more` so the model can page
  through large files (`start + returned_chars`). Content comes from
  `extracted_text`/`caption`, falling back to reading editable text files
  from disk.
- `list_files(folder_id?: int)` — folders and files (up to 100) in a folder
  or the drive root.
- `get_file_info(file_id: int)` — metadata: name, extension, size, dates,
  index status, hashtags.
- `set_hashtags(file_id: int, hashtags: string[])` — replaces the file's
  hashtags (AI word limit applies). Only used when the user explicitly asks
  for hashtags.
- `list_hashtags()` — every hashtag in use across the user's files with
  usage counts (top 50, most used first), via
  `hashtag_service.get_all_tags()`. The system prompt steers the model to
  call this first when the user mentions a tag/topic, then search with the
  exact tag terms.

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
ai_connections -> `f7a3b5c91e02` model on chat messages ->
`34b417e8a07b` per-connection rate limit -> `43d0475ce6e2` settings table
-> `cbacbe527dda` file versions -> `9f4dbb9d4221` trash (`deleted_at`)
-> `14b7197e2390` synced drives (`source_path`) -> `1c175e28cab5` sync
stats -> `00efe0293465` sync options (captions toggle, indexing workers)
-> `7a1c9e4b2d55` file_index hashtags.

## Testing

pytest suite in `tests/`, ~45 tests across 7 modules:

- `conftest.py` fixtures: `app` (fresh app with `TestConfig`, `create_all` /
  `drop_all` around each test; the app context is deliberately not kept
  pushed while clients make requests), `client` (test client), `user`
  (creates "alice", yields her id), `auth_client` (logged-in client).
- Tests mock the network: agent and AI tests patch
  `app.services.ai_service.chat_completion` with canned message dicts (tool
  calls, reasoning fields, etc.), streaming tests patch
  `chat_completion_stream` (or `requests.post` with fake SSE lines), and
  settings tests patch
  `ai_service.requests.get` for `/models` responses. No real AI backend is
  needed.
- `TestConfig` sets `INDEX_ASYNC=False`, so indexing runs inline and tests
  can assert on `file_index` rows immediately. It also sets
  `AI_STREAMING=False` so the agent tests exercise the classic path;
  streaming has dedicated tests.
- Coverage: auth, drive CRUD/upload/isolation, multi-drive behavior,
  indexing + search, file stats, AI settings (connection CRUD, env
  fallback, isolation), and the agent (multi-step runs, `read_file`
  chunking, thinking/tool_result events, token streaming, attachments,
  reasoning field, the nudge mechanism).

Run with `pytest`.

## Docker

`Dockerfile`: `python:3.11-slim` + Tesseract (eng/por), installs
requirements, and on start runs `flask --app run.py db upgrade && python
run.py`. `docker-compose.yml` exposes port 5000, loads `.env`, and mounts two
named volumes: `docindex_data` (`/app/instance`, the SQLite DB) and
`docindex_uploads` (`/app/uploads`). Start with `docker compose up --build`;
create the admin user with `docker compose exec docindex python
create_admin.py`.
