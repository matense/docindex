"""Shared HTTP helpers (framework-level, no app logic)."""

from flask import jsonify, request


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
