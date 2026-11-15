"""Pyrun module — sealed Python sandbox for the AI agent.

Agent-only in v1: no routes, no nav. ``register`` only wires the
``pyrun.compute`` / ``pyrun.save`` AI tools (see ``tools.py``).
"""


def register(ctx):
    from . import tools  # noqa: F401 - imports do the wiring

    return None
