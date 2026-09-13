"""Module system: discovery, validation, loading and enable/disable state.

Modules live in ``modules/<name>/`` with a ``module.json`` manifest:

    {
      "name": "hello",
      "version": "1.0.0",
      "description": "Example module",
      "capabilities": ["routes", "tools", "models"],
      "nav": {"label": "Hello", "icon": "fa-puzzle-piece", "url": "/"}
    }

Every valid module on disk is imported at startup and its routes are always
registered under ``/m/<name>`` with a guard that 404s while the module is
disabled — enable/disable is therefore instant and survives restarts
(persisted in the ``module_states`` table). Installing a module on disk is
the privileged trust boundary: module code runs in-process with full access.

See modules/README.md for the authoring guide.
"""

import importlib.util
import json
import logging
import os
import re
import sys

from ..extensions import db
from ..models import ModuleState

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
VALID_CAPABILITIES = {"routes", "tools", "models"}

logger = logging.getLogger(__name__)

# Discovered modules: name -> manifest dict (with "error" set when invalid).
_discovered = {}


def _modules_folder(app=None):
    from flask import current_app

    app = app or current_app
    return app.config["MODULES_FOLDER"]


def _load_manifest(path):
    """Read and validate a module directory. Returns (manifest, error)."""
    name = os.path.basename(path)
    manifest_path = os.path.join(path, "module.json")
    if not os.path.isfile(manifest_path):
        return None, "missing module.json"
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"invalid module.json: {exc}"
    if not isinstance(manifest, dict):
        return None, "module.json must be a JSON object"
    declared = manifest.get("name")
    if declared != name:
        return None, f"manifest name {declared!r} does not match folder name"
    if not NAME_RE.match(name):
        return None, "invalid module name (use lowercase letters, digits, _)"
    caps = manifest.get("capabilities", [])
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        return None, "capabilities must be a list of strings"
    unknown = set(caps) - VALID_CAPABILITIES
    if unknown:
        return None, f"unknown capabilities: {', '.join(sorted(unknown))}"
    if not os.path.isfile(os.path.join(path, "__init__.py")):
        return None, "missing __init__.py"
    manifest.setdefault("version", "")
    manifest.setdefault("description", "")
    manifest["path"] = path
    return manifest, None


def discover(app):
    """Scan the modules folder and refresh the registry. Returns the dict."""
    global _discovered
    _discovered = {}
    folder = _modules_folder(app)
    if not os.path.isdir(folder):
        return _discovered
    for entry in sorted(os.listdir(folder)):
        path = os.path.join(folder, entry)
        if not os.path.isdir(path) or entry.startswith((".", "_")):
            continue
        manifest, error = _load_manifest(path)
        if error:
            _discovered[entry] = {"name": entry, "path": path, "error": error}
            app.logger.warning("Module %r skipped: %s", entry, error)
        else:
            _discovered[entry] = manifest
    return _discovered


def _import_module(name, path):
    """Import modules/<name>/__init__.py as docindex_module_<name>."""
    fullname = f"docindex_module_{name}"
    if fullname in sys.modules:
        return sys.modules[fullname]
    init_path = os.path.join(path, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        fullname, init_path, submodule_search_locations=[path])
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    spec.loader.exec_module(module)
    return module


class ModuleContext:
    """Object handed to a module's ``register(ctx)`` entry point."""

    def __init__(self, app, name):
        self.app = app
        self.name = name
        self.db = db
        self.logger = logging.getLogger(f"docindex.module.{name}")
        self.blueprint = None


def _make_guard(name):
    def guard():
        from flask import abort
        from flask_login import current_user

        if not current_user.is_authenticated:
            from flask import redirect, request, url_for

            return redirect(url_for("auth.login", next=request.path))
        if not is_enabled(name):
            abort(404)
        return None

    guard.__name__ = f"module_guard_{name}"
    return guard


def load_all(app):
    """Discover and import every valid module, registering its routes."""
    from . import agent_service  # late import: modules call register_tool

    agent_service  # silence linters; the import primes the registry
    discover(app)
    for name, manifest in _discovered.items():
        if manifest.get("error"):
            continue
        try:
            module = _import_module(name, manifest["path"])
            ctx = ModuleContext(app, name)
            register = getattr(module, "register", None)
            bp = None
            if callable(register):
                bp = register(ctx)
            bp = bp or ctx.blueprint or getattr(module, "bp", None)
            if bp is not None:
                # Blueprint objects survive across app instances (module code
                # is imported once and cached in sys.modules), so only attach
                # the guard once — Flask rejects setup calls on a blueprint
                # that was already registered somewhere.
                if not getattr(bp, "_docindex_guarded", False):
                    bp.before_request(_make_guard(name))
                    bp._docindex_guarded = True
                app.register_blueprint(bp, url_prefix=f"/m/{name}")
            app.logger.info("Module %r loaded", name)
        except Exception:  # noqa: BLE001 - a broken module must not kill the app
            manifest["error"] = "load failed (see logs)"
            app.logger.exception("Failed to load module %r", name)
    return _discovered


def is_enabled(name):
    """True when the module is enabled (DB lookup, per request)."""
    return ModuleState.is_enabled(name)


def list_modules():
    """Manifests with runtime state, for the admin page."""
    from ..models import User

    modules = []
    for name, manifest in sorted(_discovered.items()):
        state = db.session.get(ModuleState, name)
        enabled_by = None
        if state and state.enabled_by:
            user = db.session.get(User, state.enabled_by)
            enabled_by = user.username if user else None
        modules.append({
            **{k: v for k, v in manifest.items() if k != "path"},
            "enabled": bool(state and state.enabled),
            "enabled_by": enabled_by,
            "enabled_at": state.enabled_at if state else None,
        })
    return modules


def nav_items():
    """Nav entries contributed by enabled modules, for base.html."""
    items = []
    for name, manifest in sorted(_discovered.items()):
        if manifest.get("error"):
            continue
        nav = manifest.get("nav")
        if nav and is_enabled(name):
            items.append({
                "name": name,
                "label": nav.get("label", name),
                "icon": nav.get("icon", "fa-puzzle-piece"),
                "url": f"/m/{name}{nav.get('url', '/')}",
            })
    return items


def set_enabled(name, enabled, user):
    """Persist the enable/disable flag. Returns True when the module exists."""
    if name not in _discovered or _discovered[name].get("error"):
        return False
    state = db.session.get(ModuleState, name)
    if state is None:
        state = ModuleState(name=name)
        db.session.add(state)
    state.enabled = enabled
    if enabled:
        from datetime import datetime, timezone

        state.enabled_at = datetime.now(timezone.utc)
        state.enabled_by = user.id
    db.session.commit()
    return True
