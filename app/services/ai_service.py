import base64
import threading
import time
from collections import deque

import requests
from flask import current_app

from ..models import AIConnection


class AIError(Exception):
    """Raised when the AI backend is unreachable or returns an error."""


# Sliding-window rate limiter: rate_key -> deque of request timestamps.
# In-memory is fine (single-process app); resets on restart.
_rate_windows = {}
_rate_lock = threading.Lock()


def _rate_slot(cfg, block=False):
    """Enforce the per-connection requests-per-minute limit (0 = unlimited).

    With block=True, sleep until a slot frees instead of raising — used by
    background jobs (bulk hashtag generation) so they self-throttle instead
    of failing. Shares the window with interactive requests, so both count
    towards the same per-connection limit.
    """
    limit = cfg.get("rate_limit_rpm")
    if not limit:
        return
    key = cfg.get("rate_key", "env")
    while True:
        with _rate_lock:
            window = _rate_windows.setdefault(key, deque())
            now = time.monotonic()
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) < limit:
                window.append(now)
                return
            wait = 60 - (now - window[0]) + 0.1
        if not block:
            raise AIError(
                f"Rate limit reached for this connection ({limit} requests/minute). "
                f"Wait ~{int(wait) + 1}s or raise the limit in AI Settings.")
        time.sleep(wait)


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
        "rate_limit_rpm": cfg.get("AI_RATE_LIMIT_RPM", 30),
        "streaming": cfg.get("AI_STREAMING", True),
        "rate_key": "env",
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
                # NULL falls back to the global default; 0 = unlimited.
                "rate_limit_rpm": (conn.rate_limit_rpm
                                   if conn.rate_limit_rpm is not None
                                   else current_app.config.get("AI_RATE_LIMIT_RPM", 30)),
                "streaming": current_app.config.get("AI_STREAMING", True),
                "rate_key": f"conn:{conn.id}",
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


def chat_completion(messages, model=None, tools=None, tool_choice=None, config=None,
                    block=False):
    """Call an OpenAI-compatible /chat/completions endpoint.

    Returns the first choice's message dict. With block=True, wait for a
    rate-limit slot instead of raising when the per-minute limit is reached.
    """
    cfg = config or _env_config()
    if not (cfg["enabled"] and cfg["base_url"] and cfg["model"]):
        raise AIError("AI is not configured. Add a connection in AI Settings.")

    payload = {"model": model or cfg["model"], "messages": messages}
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

    _rate_slot(cfg, block=block)

    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            json=payload,
            headers=_headers(cfg),
            timeout=cfg["timeout"],
        )
    except requests.RequestException as exc:
        raise AIError(f"Cannot reach AI backend at {cfg['base_url']}: {exc}") from exc

    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After")
        raise AIError(
            "The provider's rate limit was hit (HTTP 429)"
            + (f" — retry in ~{retry}s." if retry else ".")
            + " Lower your request rate or adjust the connection's requests/minute limit in AI Settings.")
    if resp.status_code != 200:
        raise AIError(f"AI backend returned {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
        return data["choices"][0]["message"]
    except (ValueError, KeyError, IndexError) as exc:
        raise AIError(f"Unexpected AI response format: {exc}") from exc


def chat_completion_stream(messages, model=None, tools=None, tool_choice=None,
                           config=None, block=False):
    """Streaming variant of chat_completion (SSE, OpenAI-compatible).

    Yields:
      ("token", text)     — content deltas as they arrive
      ("reasoning", text) — reasoning_content/reasoning deltas (thinking models)
      ("done", message)   — final assembled message, same shape as
                            chat_completion's return value (always last)

    Raises AIError on connection/HTTP errors. If the stream dies mid-way
    without [DONE] but content was already received, the accumulated message
    is returned as "done"; if nothing was received, AIError is raised (the
    caller may then retry with the non-streaming chat_completion).
    """
    import json as _json

    cfg = config or _env_config()
    if not (cfg["enabled"] and cfg["base_url"] and cfg["model"]):
        raise AIError("AI is not configured. Add a connection in AI Settings.")

    payload = {"model": model or cfg["model"], "messages": messages,
               "stream": True}
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

    _rate_slot(cfg, block=block)

    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            json=payload,
            headers=_headers(cfg),
            timeout=cfg["timeout"],
            stream=True,
        )
    except requests.RequestException as exc:
        raise AIError(f"Cannot reach AI backend at {cfg['base_url']}: {exc}") from exc

    if resp.status_code == 429:
        resp.close()
        retry = resp.headers.get("Retry-After")
        raise AIError(
            "The provider's rate limit was hit (HTTP 429)"
            + (f" — retry in ~{retry}s." if retry else ".")
            + " Lower your request rate or adjust the connection's requests/minute limit in AI Settings.")
    if resp.status_code != 200:
        excerpt = resp.text[:300]
        resp.close()
        raise AIError(f"AI backend returned {resp.status_code}: {excerpt}")

    content_parts = []
    reasoning_parts = []
    tool_calls = {}  # index -> {"id", "type", "function": {"name", "arguments"}}
    got_anything = False

    try:
        # Iterate raw bytes and decode as UTF-8 explicitly — SSE servers often
        # omit the charset header and requests would otherwise decode deltas
        # as latin-1, mangling non-ASCII text ("análise" → "anÃ¡lise").
        for raw in resp.iter_lines():
            if not raw:
                continue  # keep-alive / blank separator lines
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk = _json.loads(data_str)
            except ValueError:
                continue  # tolerate junk lines from proxies
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            got_anything = True

            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
                yield ("token", piece)

            think = delta.get("reasoning_content") or delta.get("reasoning")
            if think:
                reasoning_parts.append(think)
                yield ("reasoning", think)

            for tc in delta.get("tool_calls") or []:
                slot = tool_calls.setdefault(
                    tc.get("index", 0),
                    {"id": "", "type": "function",
                     "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
    except requests.RequestException as exc:
        if not got_anything:
            raise AIError(
                f"Lost connection to AI backend at {cfg['base_url']}: {exc}") from exc
        # Mid-stream drop with partial content: fall through and finish with
        # what we have — better than killing the answer entirely.
    finally:
        resp.close()

    if not got_anything:
        raise AIError("AI backend closed the stream without any content.")

    message = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    yield ("done", message)


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
