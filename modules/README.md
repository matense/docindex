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
