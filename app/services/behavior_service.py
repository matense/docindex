"""User-made behaviors — reusable AI instructions ("system prompts").

Core feature (moved out of the Automation module): behaviors are picked in
the AI nodes' behavior field and can be listed/saved by the AI itself via
the core agent tools. The ``enabled`` flag hides a behavior from pickers
without deleting it.
"""

from ..extensions import db
from ..models import Behavior


def list_behaviors(user_id, enabled_only=False):
    q = (Behavior.query.filter_by(user_id=user_id)
         .order_by(Behavior.name))
    if enabled_only:
        q = q.filter_by(enabled=True)
    return q.all()


def get_behavior(behavior_id, user_id):
    behavior = db.session.get(Behavior, behavior_id)
    if behavior is None or behavior.user_id != user_id:
        return None
    return behavior


def create_behavior(user_id, name, content):
    behavior = Behavior(user_id=user_id, name=name, content=content)
    db.session.add(behavior)
    db.session.commit()
    return behavior


def update_behavior(behavior, name, content):
    behavior.name = name
    behavior.content = content
    db.session.commit()
    return behavior


def set_enabled(behavior, enabled):
    behavior.enabled = bool(enabled)
    db.session.commit()
    return behavior.enabled


def set_favorite(behavior, favorite):
    behavior.favorite = bool(favorite)
    db.session.commit()
    return behavior.favorite


def delete_behavior(behavior):
    db.session.delete(behavior)
    db.session.commit()


def save_by_name_or_id(user_id, name, content, behavior_id=None):
    """Upsert used by the AI's save_behavior tool: update by id when given,
    otherwise by name, otherwise create. Returns (behavior, action)."""
    behavior = None
    if behavior_id not in (None, ""):
        try:
            behavior = get_behavior(int(behavior_id), user_id)
        except (TypeError, ValueError):
            behavior = None
        if behavior is None:
            return None, "not_found"
    if behavior is None:
        behavior = (Behavior.query
                    .filter_by(user_id=user_id, name=name).first())
    if behavior is None:
        return create_behavior(user_id, name, content), "created"
    return update_behavior(behavior, name, content), "updated"
