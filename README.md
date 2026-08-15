# DocIndex

A personal, Google-like searchable file drive. Upload documents and images,
and DocIndex indexes their full text — including AI-generated captions for
images — so you can search everything. A built-in AI assistant answers
questions about your files by searching and reading them step by step.

## Features

- **File drive** — folders, multi-file upload, download, rename, move, delete,
  and in-place editing of text/code files.
- **Full-text indexing** — text is extracted from PDFs (pdfplumber, incl.
  tables), DOCX (python-docx), text/code files, and images (OCR via
  Tesseract). Everything lands in a searchable database index.
- **AI image captioning** — images are described by a vision model so their
  content is searchable too.
- **Instant search** — Google-style search dock with live results (Alt+S),
  highlighted snippets and full search page.
- **AI assistant** — agentic chat that searches, reads and cross-references
  your files in multiple steps, then answers with cited sources. Multiple AI
  connections (models) can be configured and switched per conversation.

## Tech

Python 3.11+ / Flask, SQLAlchemy + Flask-Migrate (SQLite by default),
Flask-Login, Flask-WTF. Frontend: Tailwind CSS + DaisyUI via CDN, vanilla JS.
AI: any OpenAI-compatible endpoint (Ollama, LM Studio, OpenAI API, ...).

## Installation (local)

Requirements: Python 3.11+. Optional:
[Tesseract](https://github.com/tesseract-ocr/tesseract) for OCR of images.

```bash
# 1. Clone and enter the project
git clone https://github.com/matense/docindex.git
cd docindex

# 2. Create a virtual environment and install dependencies
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure the environment
cp .env.example .env            # Windows: copy .env.example .env
# Edit .env and set SECRET_KEY to a long random string.

# 4. Create the database
flask --app run.py db upgrade

# 5. Create the first admin user (prompts for username, email, password)
python create_admin.py

# 6. Run
python run.py                   # http://localhost:5000
```

For OCR of images, install the Tesseract binary and set `TESSERACT_LANGS`
(default `eng+por`) in `.env`.

For development with auto-reload, add `FLASK_DEBUG=true` to your local `.env`
(never enable this in production).

## Docker

Requirements: Docker with the Compose plugin.

```bash
# 1. Clone and configure
git clone https://github.com/matense/docindex.git
cd docindex
cp .env.example .env            # set SECRET_KEY (and AI settings if wanted)

# 2. Build and start (database migrations run automatically on start)
docker compose up --build -d

# 3. Create the first admin user inside the container
docker compose exec docindex python create_admin.py
```

The app is at http://localhost:5000. The database and uploaded files persist
in the named volumes `docindex_data` and `docindex_uploads`.

If your AI server (Ollama, LM Studio) runs on the **host** machine, use
`http://host.docker.internal:<port>/v1` as the base URL — `localhost` inside
the container refers to the container itself. In LM Studio, enable
"Serve on local network" for this to work.

## AI configuration

AI can be configured in two places:

- **AI Settings page** (in the app, admin users): create named connections
  (base URL, API key, chat model, vision model, max agent steps), test them,
  and activate the one the assistant uses. This is the recommended way.
- **`.env` defaults** — used to seed the first connection and as fallback:

| Variable            | Description                                    | Default                     |
|---------------------|------------------------------------------------|-----------------------------|
| `AI_ENABLED`        | `true` to enable AI features                   | `false`                     |
| `AI_BASE_URL`       | OpenAI-compatible base URL                     | `http://localhost:11434/v1` |
| `AI_API_KEY`        | API key (empty for local servers)              | —                           |
| `AI_MODEL`          | Chat model for the assistant                   | `llama3.1`                  |
| `AI_VISION_MODEL`   | Vision model for image captions (fallback: `AI_MODEL`) | —                   |
| `AI_MAX_STEPS`      | Max tool-call steps per question (overridable per connection) | `16` |
| `AI_REQUEST_TIMEOUT`| AI request timeout in seconds                  | `300`                       |

Examples:

- **Ollama**: `AI_BASE_URL=http://localhost:11434/v1`, `AI_MODEL=llama3.1`,
  `AI_VISION_MODEL=llava`
- **LM Studio**: `AI_BASE_URL=http://localhost:1234/v1`,
  `AI_MODEL=<model-id-shown-in-lm-studio>`
- **OpenAI**: `AI_BASE_URL=https://api.openai.com/v1`, `AI_API_KEY=sk-...`,
  `AI_MODEL=gpt-4o-mini`, `AI_VISION_MODEL=gpt-4o-mini`

## Tests

```bash
pytest
```

## Project layout

```
app/
  models.py          # User, Drive, Folder, StoredFile, FileIndex, AIConnection, Chat*
  routes/            # auth, drive, search, ai, settings blueprints
  services/          # business logic (drive, file, indexing, search, ai, agent)
  static/js/         # SPA, drive UI, AI chat (dock + full page)
  templates/         # Jinja2 templates
migrations/          # Flask-Migrate (Alembic) schema versions
create_admin.py      # CLI: create the first admin user
run.py               # entry point
DOCUMENTATION.md     # detailed internal technical documentation
CONTRIBUTING.md      # development rules and workflow (read before contributing)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow —
database migrations, git flow, and the rules that keep existing
installations compatible.

## License

[MIT](LICENSE)
