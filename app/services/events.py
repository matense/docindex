"""Internal event bus: minimal synchronous pub/sub for automation triggers.

Consumers import exactly these two names (kept stable for P5)::

    from app.services import events
    events.subscribe("file.uploaded", fn)
    events.emit("file.uploaded", {"file_id": ..., "drive_id": ...,
                                  "user_id": ...})

Payloads are plain JSON-serializable dicts. One bad subscriber never
breaks the emitter: subscriber failures are logged (via ``app.logger``
inside an app context) and delivery continues.
"""

import logging

from flask import current_app, has_app_context

_log = logging.getLogger(__name__)

# event name -> [callables]; synchronous, in-process only.
_SUBSCRIBERS = {}


def subscribe(event_name, fn):
    """Register ``fn(payload)`` for ``event_name``. Returns ``fn``."""
    _SUBSCRIBERS.setdefault(event_name, []).append(fn)
    return fn


def _log_subscriber_failure(event_name):
    """Log a subscriber crash; never raises, even without an app context."""
    try:
        if has_app_context():
            current_app.logger.exception(
                "Event subscriber failed for %r", event_name)
            return
    except Exception:  # noqa: BLE001 - logging must never break emit
        pass
    _log.exception("Event subscriber failed for %r", event_name)


def emit(event_name, payload):
    """Call each ``event_name`` subscriber with ``payload``.

    Never raises for subscriber errors or a missing app context — a
    failing subscriber is logged and delivery continues with the rest.
    Emitting an event with no subscribers is a no-op.
    """
    for fn in list(_SUBSCRIBERS.get(event_name, []) or []):
        try:
            fn(payload)
        except Exception:  # noqa: BLE001 - isolation: keep delivering
            _log_subscriber_failure(event_name)
