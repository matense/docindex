# DocIndex modules

Modules extend DocIndex with extra pages, AI tools and database tables —
without touching the core code. Drop a folder here, restart the app, and
enable it under **Profile → Manage modules** (admin only).

> **Trust model — read this before installing anything.**
> Module code runs **in-process with full access** to the app, the database
> and the filesystem. There is no sandbox. Putting a module folder on disk is
> the privileged action: only install modules you wrote or trust, and review
> the code first. The enable/disable toggle only controls *exposure* (routes
> 404 and AI tools disappear while disabled) — it is not a security boundary.

## Layout

```
modules/
└── hello/                  <- the module name (lowercase, digits, _)
    ├── module.json         <- manifest (required)
    ├── __init__.py         <- register(ctx) entry point (required)
    ├── routes.py           <- optional Flask blueprint
    ├── tools.py            <- optional AI tools
    ├── models.py           <- optional SQLAlchemy models
    ├── templates/          <- blueprint templates
    └── migrations/
        └── versions/       <- alembic revisions (independent root)
```

## module.json

```json
{
  "name": "hello",
  "version": "1.0.0",
  "description": "What the module does.",
  "capabilities": ["routes", "tools", "models"],
  "nav": {"label": "Hello", "icon": "fa-puzzle-piece", "url": "/"}
}
```

- `name` must match the folder name and match `^[a-z][a-z0-9_]*$`.
- `capabilities` is a whitelist — only `routes`, `tools`, `models` are valid.
  Unknown capabilities make the module be skipped (with a log warning).
- `nav` is optional: when the module is enabled, the entry shows up in the
  profile dropdown under "Modules". `url` is relative to `/m/<name>`.
- `help` is optional: a list of paragraphs shown on the app's help page
  (Settings → Help) while the module is enabled, under its `description`.

## Entry point

`__init__.py` must define `register(ctx)`:

```python
def register(ctx):
    from . import models, routes, tools  # imports do the wiring

    ctx.blueprint = routes.bp
    return ctx.blueprint
```

`ctx` provides `ctx.app`, `ctx.name`, `ctx.db` (the shared SQLAlchemy
instance), `ctx.logger` and `ctx.blueprint`. Return the blueprint (or assign
`ctx.blueprint`) to expose routes.

Optionally define `on_enable(user)`: called every time an admin enables the
module, with the user who toggled it. Use it to create default content —
e.g. the notebooks module creates a "My Notebooks" drive. Keep it
idempotent; exceptions are logged and never break the toggle.

## Routes

Define a normal Flask blueprint. All routes are mounted under
`/m/<module-name>/` and require login; while the module is disabled they
return 404. Templates live in the module's own `templates/` folder and can
extend the app's `base.html`.

## AI tools

Register tools with the agent's registry — module tools are namespaced as
`<module>.<tool>` and are only offered to the model while the module is
enabled:

```python
from app.services.agent_service import register_tool

register_tool(
    "echo",
    {"type": "function", "function": {
        "name": "hello.echo",
        "description": "Echo text back.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    lambda user, args, drive: {"echo": args.get("text", "")},
    label="Echoed",          # shown in the chat UI while the tool runs
    module="hello")
```

Tool names collide loudly: registering a duplicate raises `ValueError` and
the module is skipped.

## Search integration and file viewers

Modules can hook the indexing pipeline and the file viewer:

```python
from app.services import indexing_service, module_service

indexing_service.register_extractor("pdocnb", extract_fn)
# extract_fn(stored_file) -> plain text; consulted by extract_text() before
# the built-in dispatch, so reindex/sync/queue all work for your file type.

module_service.register_file_viewer(
    "pdocnb", lambda stored: f"/m/mymodule/{stored.id}", module="mymodule")
# GET /file/<id>/view redirects to your viewer while the module is enabled;
# disabled modules fall back to the generic viewer.
```

Prefer storing module documents as regular files (see `modules/notebooks/`):
they get ownership, drives/folders, trash, `FileVersion` history, FTS search
and the AI agent's core tools (search/read/grep/hashtags) for free.

## AI file guards

Modules can make files invisible (or read-only) to the AI agent:

```python
from app.services import agent_service

agent_service.register_file_guard(guard_fn, module="mymodule")
# guard_fn(user, stored_file) -> error message str, or None to allow access.
```

A guarded file is refused by the core `read_file` / `grep_file` /
`get_file_info` tools and dropped from `search_files` / `list_files`
results. Module guards apply only while the module is enabled. The
notebooks module uses this for its "hide from AI" flag; its own tools
additionally check a write-mode flag for the "lock AI edits" toggle.

## Database tables

- Table names must be prefixed `mod_<module>_` (e.g. `mod_hello_note`).
- Migrations live in `migrations/versions/` inside the module as an
  **independent alembic root**: `down_revision = None`,
  `branch_labels = ("<module>",)`, and a revision id prefixed with the
  module name (e.g. `hello_0001`). Never chain a module migration onto a
  core revision — removing the module from disk must never break core
  upgrades.
- Core and module migrations are aggregated automatically; run
  `flask --app run.py db upgrade heads` (note: `heads`, plural) to apply
  everything.

## Lifecycle

- All valid modules on disk are imported at startup; enable/disable is
  instant and persisted in the `module_states` table.
- A module that fails validation or import is skipped and listed with its
  error on the modules page — it never takes the app down.
- See `modules/hello/` for a complete working example.
