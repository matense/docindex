"""Tables module — queryable datasets stored as .ptable files."""

# Viewer-owned extensions: the native .ptable format plus the importable
# spreadsheet types, which open in the tables viewer (T2).
TABLE_EXTENSIONS = ("csv", "tsv", "xlsx", "ptable")


def register(ctx):
    from app.services import module_service

    from . import doc, routes, tools  # noqa: F401 - imports do the wiring

    for extension in TABLE_EXTENSIONS:
        module_service.register_file_viewer(
            extension,
            lambda stored: f"/m/tables/{stored.id}",
            module="tables")
    ctx.blueprint = routes.bp
    return ctx.blueprint


def on_enable(user):
    """First-enable hook: tables live on the user's own drives, so — as a
    deliberate deviation from the dashboards/whiteboards precedent — no
    dedicated drive is created here."""
    return None
