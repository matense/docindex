"""AI tool contributed by the hello module (namespaced as hello.echo)."""

from app.services.agent_service import register_tool

_DEFINITION = {
    "type": "function",
    "function": {
        "name": "hello.echo",
        "description": "Echo text back. Example tool from the hello module.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to echo."},
            },
            "required": ["text"],
        },
    },
}


def _handler(user, args, drive=None):
    return {"echo": args.get("text", "")}


register_tool("echo", _DEFINITION, _handler, label="Echoed", module="hello")
