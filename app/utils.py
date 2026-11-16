"""Shared HTTP helpers (framework-level, no app logic)."""

from urllib.parse import urlparse

from flask import jsonify, request


def safe_local_redirect(value, fallback):
    """Return a same-origin path-only redirect target, else `fallback`.

    Rejects protocol-relative URLs (``//evil.com``), absolute URLs to other
    hosts, schemes (``javascript:``/``data:``), backslashes (browsers
    normalize ``/\\`` to ``//``), encoded separators (``%2f``/``%5c``) and
    header-injection control chars. Absolute http(s) URLs on the *current*
    host (e.g. a ``Referer``) are reduced to their path+query.
    """
    if not value or not isinstance(value, str):
        return fallback
    value = value.strip()
    if not value:
        return fallback
    if "\\" in value or "\n" in value or "\r" in value or "\x00" in value:
        return fallback
    lowered = value.lower()
    if "%2f" in lowered or "%5c" in lowered or "%00" in lowered:
        return fallback
    parts = urlparse(value)
    if parts.scheme or parts.netloc:
        if parts.scheme not in ("http", "https"):
            return fallback
        try:
            host = request.host.lower()
        except Exception:  # noqa: BLE001 - no request context in some tests
            return fallback
        if parts.netloc.lower() != host:
            return fallback
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        if (not target.startswith("/") or target.startswith("//")
                or "\\" in target):
            return fallback
        return target
    if not value.startswith("/") or value.startswith("//"):
        return fallback
    return value


def wants_json(req=None):
    """True when the caller expects JSON instead of HTML.

    Matches the convention already used by the JS clients, which send
    ``Accept: application/json`` on data fetches, plus plain JSON bodies.
    Used to branch twin responses (JSON vs redirect/flash) and to return
    401 JSON instead of a login redirect.
    """
    req = req if req is not None else request
    try:
        if req.is_json:
            return True
    except Exception:  # noqa: BLE001 - malformed content-type etc.
        pass
    accept = ""
    try:
        accept = req.headers.get("Accept", "") or ""
    except Exception:  # noqa: BLE001 - no request context in some tests
        pass
    return "application/json" in accept


def json_error(message, status=400):
    """Standard error envelope shared by JSON twins: ``{"error": msg}``."""
    return jsonify({"error": message}), status
