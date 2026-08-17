# Contributing to DocIndex

Rules and workflows for developing DocIndex while keeping existing
installations compatible. Read this before making changes.

## Ground rules

1. **Database changes always go through migrations** — never edit the
   database directly or ship code that requires manual schema fixes.
2. **Run the tests before every push** — `pytest` must be green (78+ tests).
3. **Never commit secrets or runtime data** — `.env`, `instance/`,
   `uploads/` and `venv/` are gitignored; keep it that way.
4. **Keep the docs in sync** — if you change the architecture, update
   `DOCUMENTATION.md` and `.github/copilot-instructions.md` in the same
   commit.

## Development setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # set SECRET_KEY; add FLASK_DEBUG=true for auto-reload
flask --app run.py db upgrade
python create_admin.py
python run.py
```

## Database compatibility (migrations)

Any change to `app/models.py` must ship with an Alembic migration:

```bash
# 1. Change the model in app/models.py
# 2. Generate the migration
flask --app run.py db migrate -m "short description"
# 3. REVIEW the generated file in migrations/versions/ —
#    autogenerate can miss details (data migrations, server defaults, ...)
# 4. Apply it locally
flask --app run.py db upgrade
```

Commit the migration file **together with the code that needs it**. This is
what keeps every installation compatible:

- **Fresh install** — `db upgrade` runs all migrations in order.
- **Existing install** — `db upgrade` upgrades the database in place, no data
  loss. Docker does this automatically when the container starts.

## Git workflow

```bash
git add -A
git commit -m "description of the change"
git push
```

- Use feature branches for larger work: `git checkout -b feature/my-thing`.
- Tag releases: `git tag v0.2 && git push --tags`.
- Never force-push to `main` (the one-time `--force` at the initial cleanup
  was the exception).

## Dependencies

- Install with `pip install <package>`, then add it to `requirements.txt`
  **with a pinned version** (`package==1.2.3`). The Docker build depends on
  this file.

## Configuration

- New settings go in `config.py`, read via `os.environ.get` with a sensible
  default.
- Document them in `.env.example` (never commit a real `.env`).

## Code conventions

- **Backend**: business logic lives in `app/services/` (static methods);
  keep routes thin. Protect routes with `@login_required`.
- **Frontend**: vanilla ES6+ in `app/static/js/`, Tailwind + DaisyUI classes,
  templates extend `base.html`.
- **Dialogs**: never use native `alert()` / `confirm()` / `prompt()`. Use the
  global modal in `app/static/js/ui.js`: `await window.uiConfirm(msg, { danger: true })`
  in JS, or declarative `data-confirm="..."` (+ optional `data-confirm-danger`)
  attributes on forms/links.
- **Tests**: add tests for new features — `tests/conftest.py` provides the
  app/client fixtures with an in-memory database.

## Checklist before pushing

- [ ] `pytest` passes
- [ ] model changes include a reviewed migration in `migrations/versions/`
- [ ] new dependencies pinned in `requirements.txt`
- [ ] new settings documented in `.env.example`
- [ ] `DOCUMENTATION.md` / `copilot-instructions.md` updated if architecture changed
