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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Debug (auto-reload) is opt-in: set FLASK_DEBUG=true in your local .env.
    debug = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)
