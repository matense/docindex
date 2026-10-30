"""Measured AI token spend: normalize, record and aggregate.

Source of truth is the provider's own ``usage`` payload
(``prompt_tokens``/``completion_tokens`` on OpenAI-compatible
``/chat/completions`` responses). When a provider omits it, the row is
estimated (chars//4) and flagged ``estimated=True`` so the UI can say so.
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import func

from ..models import AIUsageEvent, db


def _estimate_text_tokens(text):
    """Rough token estimate for free text (~4 chars per token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_prompt_tokens(messages):
    """Estimate prompt tokens from the request messages (chars//4)."""
    total = 0
    for m in messages or []:
        content = m.get("content", "")
        if isinstance(content, list):
            # Multimodal content blocks (text + image_url).
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += _estimate_text_tokens(part.get("text") or "")
        else:
            total += _estimate_text_tokens(content or "")
        tool_calls = m.get("tool_calls") or []
        for tc in tool_calls:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            total += _estimate_text_tokens(str(fn.get("arguments") or ""))
    return total


def normalize_usage(raw, messages=None, content=""):
    """Build a usage dict from a provider ``usage`` payload.

    Provider numbers win per-field; missing fields are estimated and the
    result is flagged ``estimated=True`` when anything was estimated.
    Always returns ints >= 0.
    """
    raw = raw if isinstance(raw, dict) else {}
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    estimated = False
    if not isinstance(prompt, int) or prompt < 0:
        prompt = estimate_prompt_tokens(messages or [])
        estimated = True
    if not isinstance(completion, int) or completion < 0:
        completion = _estimate_text_tokens(content or "")
        estimated = True
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "estimated": estimated,
    }


def record_usage(user_id, usage, model=None, kind="chat",
                 conversation_id=None, task_id=None, message_id=None):
    """Persist one measured LLM call. Never raises (usage tracking must
    not break the AI answer it measures)."""
    if not user_id or not isinstance(usage, dict):
        return None
    try:
        row = AIUsageEvent(
            user_id=user_id,
            conversation_id=conversation_id,
            task_id=task_id,
            message_id=message_id,
            kind=kind or "chat",
            model=model,
            prompt_tokens=max(0, int(usage.get("prompt_tokens") or 0)),
            completion_tokens=max(0, int(usage.get("completion_tokens") or 0)),
            estimated=bool(usage.get("estimated")),
        )
        db.session.add(row)
        db.session.commit()
        return row
    except Exception:  # noqa: BLE001 - tracking is best-effort
        db.session.rollback()
        return None


def message_usage_map(conversation_id):
    """Per-assistant-message token totals for one conversation.

    Returns {message_id: {"prompt_tokens", "completion_tokens",
    "estimated"}} summing every linked LLM call (a message backed by
    several agent steps sums them all).
    """
    rows = (db.session.query(
                AIUsageEvent.message_id,
                func.coalesce(func.sum(AIUsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(AIUsageEvent.completion_tokens), 0),
                func.max(AIUsageEvent.estimated))
            .filter(AIUsageEvent.conversation_id == conversation_id,
                    AIUsageEvent.message_id.isnot(None))
            .group_by(AIUsageEvent.message_id).all())
    return {mid: {"prompt_tokens": int(p), "completion_tokens": int(c),
                  "estimated": bool(e)}
            for mid, p, c, e in rows}


def _range_sums(user_id, since):
    row = (db.session.query(
                func.coalesce(func.sum(AIUsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(AIUsageEvent.completion_tokens), 0),
                func.count(AIUsageEvent.id))
            .filter(AIUsageEvent.user_id == user_id,
                    AIUsageEvent.created_at >= since).first())
    return {"prompt_tokens": int(row[0]), "completion_tokens": int(row[1]),
            "calls": int(row[2]),
            "total_tokens": int(row[0]) + int(row[1])}


def get_stats(user_id, days=30):
    """Token totals for today, last 7 days, last N days and all time,
    plus a per-day series (oldest first) for the mini-chart."""
    now = datetime.now(UTC).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stats = {
        "today": _range_sums(user_id, today_start),
        "week": _range_sums(user_id, today_start - timedelta(days=6)),
        "month": _range_sums(user_id, today_start - timedelta(days=days - 1)),
        "total": _range_sums(user_id, datetime(1970, 1, 1)),
    }
    day_rows = (db.session.query(
                    func.date(AIUsageEvent.created_at),
                    func.coalesce(func.sum(AIUsageEvent.prompt_tokens), 0),
                    func.coalesce(func.sum(AIUsageEvent.completion_tokens), 0))
                .filter(AIUsageEvent.user_id == user_id,
                        AIUsageEvent.created_at >= today_start - timedelta(days=days - 1))
                .group_by(func.date(AIUsageEvent.created_at)).all())
    by_day = {str(d): (int(p), int(c)) for d, p, c in day_rows}
    series = []
    for i in range(days - 1, -1, -1):
        day = (today_start - timedelta(days=i)).date().isoformat()
        p, c = by_day.get(day, (0, 0))
        series.append({"day": day, "prompt_tokens": p,
                       "completion_tokens": c, "total_tokens": p + c})
    stats["daily"] = series
    return stats


def format_tokens(n):
    """Compact human format: 950 -> '950', 12400 -> '12.4k', 2M -> '2.0M'."""
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        v = n / 1000
        return f"{v:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
