import os

import click

from app import create_app

app = create_app()


@app.cli.command("reindex-fts")
def reindex_fts():
    """Rebuild the FTS5 search index from file_index (safe to run anytime)."""
    from app.services import search_service
    if not search_service.fts_available():
        click.echo("FTS5 is not available (or SEARCH_FTS=false) — nothing to do.")
        return
    n = search_service.fts_rebuild()
    click.echo(f"FTS index rebuilt: {n} file(s) indexed.")


@app.cli.command("db-repair")
def db_repair():
    """Recover from "Can't locate revision identified by ..." on db upgrade.

    Databases created by the first public release carry migration stamps
    that no longer exist. This creates any missing tables (existing data is
    untouched) and re-stamps the database at the current migration head.
    """
    from app.services import db_maintenance
    db_maintenance.repair_unknown_revision(app)
    click.echo("Database repaired and re-stamped at the current migration head.")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Debug (auto-reload) is opt-in: set FLASK_DEBUG=true in your local .env.
    debug = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)
