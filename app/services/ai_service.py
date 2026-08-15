import base64

import requests
from flask import current_app

from ..models import AIConnection


class AIError(Exception):
    """Raised when the AI backend is unreachable or returns an error."""


def _env_config():
    cfg = current_app.config
    return {
        "enabled": cfg.get("AI_ENABLED", False),
        "base_url": (cfg.get("AI_BASE_URL") or "").rstrip("/"),
        "api_key": cfg.get("AI_API_KEY", ""),
        "model": cfg.get("AI_MODEL", ""),
        "vision_model": cfg.get("AI_VISION_MODEL") or cfg.get("AI_MODEL", ""),
        "timeout": cfg.get("AI_REQUEST_TIMEOUT", 120),
        "max_steps": cfg.get("AI_MAX_STEPS", 16),
    }


def config_for(user=None):
    """Resolve the AI config: the user's active connection wins, env fallback."""
    if user is not None:
        conn = AIConnection.query.filter_by(user_id=user.id, is_active=True).first()
        if conn:
            return {
                "enabled": True,
                "base_url": conn.base_url.rstrip("/"),
                "api_key": conn.api_key or "",
                "model": conn.model,
                "vision_model": conn.vision_model or conn.model,
                "timeout": current_app.config.get("AI_REQUEST_TIMEOUT", 120),
                # NULL on the connection falls back to the global default.
                "max_steps": conn.max_steps
                             or current_app.config.get("AI_MAX_STEPS", 16),
            }
    return _env_config()


def is_enabled(user=None):
    cfg = config_for(user)
    return cfg["enabled"] and bool(cfg["base_url"]) and bool(cfg["model"])


def _headers(cfg):
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    return headers


def chat_completion(messages, model=None, tools=None, tool_choice=None, config=None):
    """Call an OpenAI-compatible /chat/completions endpoint.

    Returns the first choice's message dict.
    """
    cfg = config or _env_config()
    if not (cfg["enabled"] and cfg["base_url"] and cfg["model"]):
        raise AIError("AI is not configured. Add a connection in AI Settings.")

    payload = {"model": model or cfg["model"], "messages": messages}
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            json=payload,
            headers=_headers(cfg),
            timeout=cfg["timeout"],
        )
    except requests.RequestException as exc:
        raise AIError(f"Cannot reach AI backend at {cfg['base_url']}: {exc}") from exc

    if resp.status_code != 200:
        raise AIError(f"AI backend returned {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
        return data["choices"][0]["message"]
    except (ValueError, KeyError, IndexError) as exc:
        raise AIError(f"Unexpected AI response format: {exc}") from exc


def list_models(base_url, api_key):
    """Fetch available model ids from the provider. Returns (ok, models_or_error)."""
    cfg = {"base_url": base_url.rstrip("/"), "api_key": api_key or "", "timeout": 15}
    try:
        resp = requests.get(f"{cfg['base_url']}/models",
                            headers=_headers(cfg), timeout=cfg["timeout"])
    except requests.RequestException as exc:
        return False, f"Cannot reach {cfg['base_url']}: {exc}"
    if resp.status_code != 200:
        return False, f"Provider returned {resp.status_code}: {resp.text[:200]}"
    try:
        models = sorted(m["id"] for m in resp.json().get("data", []) if m.get("id"))
    except ValueError:
        return False, "Provider did not return a valid model list."
    return True, models


def test_connection(base_url, api_key):
    """Ping the provider's /models endpoint. Returns (ok, message)."""
    ok, result = list_models(base_url, api_key)
    if not ok:
        return False, result
    if result:
        return True, "Connected. Models: " + ", ".join(result[:8])
    return True, "Connected."


def caption_image(path, config=None):
    """Generate a searchable caption for an image via a vision model."""
    cfg = config or _env_config()
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")

    ext = path.rsplit(".", 1)[-1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, f"image/{ext}")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Describe this image in detail so it can be found via text "
                        "search. Mention objects, people, scenes, colors, any visible "
                        "text, and the overall context. Answer with the caption only."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
            ],
        }
    ]
    message = chat_completion(messages, model=cfg["vision_model"], config=cfg)
    return (message.get("content") or "").strip()
