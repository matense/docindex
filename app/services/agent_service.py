import json
import re

from flask import current_app

from ..models import StoredFile
from . import ai_service, file_service, search_service

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Full-text search over the user's file drive. Searches filenames, "
                "extracted document text and AI-generated image captions. "
                "Returns matching files with snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the extracted text or caption of a file by its id. Large "
                "files are returned in chunks: use 'start' (character offset) and "
                "'length' to page through them, and check 'has_more' and "
                "'total_chars' in the result to know if you should continue reading."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "integer", "description": "The file id."},
                    "start": {
                        "type": "integer",
                        "description": "Character offset to start reading from (default 0).",
                    },
                    "length": {
                        "type": "integer",
                        "description": "Max characters to return (default 20000, max 50000).",
                    },
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List the files and folders in the user's drive, optionally inside "
                "one folder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "integer",
                        "description": "Folder id to list; omit for the root.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_info",
            "description": "Get metadata (name, size, type, dates) for a file by its id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "integer", "description": "The file id."},
                },
                "required": ["file_id"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are the assistant of DocIndex, a personal file drive.
Answer the user's questions using the knowledge stored in their files.

Rules:
- Work step by step. Do NOT try to answer in one go.
- IMPORTANT: before every tool call, ALWAYS write one or two short sentences
  of reasoning as plain message content — explain what you are looking for and
  why. Never call a tool without stating your reasoning first.
- First call search_files with a broad query, then read_file on the most
  relevant results, then search again with refined terms if needed.
- Prefer a single tool call per step; wait for the result before continuing.
- Large files are read in chunks: when read_file returns has_more=true, call
  it again with start set to start + returned_chars to continue reading.
  Continue until you have the information you need.
- Always cite the files you used by name, like [filename](file://ID).
- Some files are marked read_only=true — they mirror a real folder on disk
  and cannot be edited, moved or deleted. Never offer to modify them.
- If the files don't contain the answer, say so honestly — never invent content.
- Answer in the same language the user writes in."""


# Smaller/local models sometimes narrate the next step ("Vou ler o ficheiro…",
# "Let me read the file…") without emitting the tool call in the same
# response. When that happens the text looks like a final answer but is really
# intermediate reasoning, so we detect the stated intent and nudge the model
# to continue instead of ending the task mid-way.
_INTENT_RE = re.compile(
    r"(\bvou\b|\bvamos\b|\birei\b|\bde seguida\b|\bpreciso\b"
    r"|\blet me\b|\bi will\b|\bi'll\b|\bi'm going to\b|\bi need to\b"
    r"|\bi should\b|\bnext\b"
    r"|\bvoy a\b|\bvamos a\b|\bnecesito\b)",
    re.IGNORECASE,
)

_NUDGE = ("You described what you plan to do next but did not call any tool. "
          "Continue the task now: call the next tool if you still need "
          "information, or write the final answer if you are done.")

_MAX_NUDGES = 4


def _tool_search_files(user, query, drive=None):
    results = search_service.search_files(query, user, limit=10, drive=drive)
    return [
        {
            "file_id": r["file"].id,
            "name": r["file"].name,
            "snippet": r["snippet"],
        }
        for r in results
    ]


def _tool_read_file(user, file_id, start=0, length=20_000):
    stored = (StoredFile.query
              .filter_by(id=file_id, user_id=user.id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if not stored:
        return {"error": "File not found."}

    content = ""
    index = stored.index
    if index:
        content = index.extracted_text or index.caption or ""
    if not content and stored.is_editable:
        try:
            content = file_service.read_text_content(stored, max_chars=500_000)
        except OSError:
            pass
    if not content:
        return {"file_id": stored.id, "name": stored.name,
                "content": "", "note": "No text content available for this file."}

    start = max(0, int(start or 0))
    length = min(max(1, int(length or 20_000)), 50_000)
    total = len(content)
    chunk = content[start:start + length]

    return {
        "file_id": stored.id,
        "name": stored.name,
        "start": start,
        "returned_chars": len(chunk),
        "total_chars": total,
        "has_more": start + len(chunk) < total,
        "content": chunk,
    }


def _tool_list_files(user, folder_id=None, drive=None):
    from ..models import Folder

    query = (StoredFile.query
             .filter_by(user_id=user.id)
             .filter(StoredFile.deleted_at.is_(None)))
    folders_q = Folder.query.filter_by(user_id=user.id)
    if drive is not None:
        query = query.filter(StoredFile.drive_id == drive.id)
        folders_q = folders_q.filter(Folder.drive_id == drive.id)
    if folder_id:
        query = query.filter_by(folder_id=folder_id)
        folders_q = folders_q.filter_by(parent_id=folder_id)
    else:
        query = query.filter(StoredFile.folder_id.is_(None))
        folders_q = folders_q.filter(Folder.parent_id.is_(None))
    return {
        "folders": [{"folder_id": f.id, "name": f.name} for f in folders_q.all()],
        "files": [
            {"file_id": f.id, "name": f.name, "size": f.size,
             "read_only": f.is_synced}
            for f in query.limit(100).all()
        ],
    }


def _tool_get_file_info(user, file_id):
    stored = (StoredFile.query
              .filter_by(id=file_id, user_id=user.id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if not stored:
        return {"error": "File not found."}
    return {
        "file_id": stored.id,
        "name": stored.name,
        "extension": stored.extension,
        "size": stored.size,
        "created_at": stored.created_at.isoformat() if stored.created_at else None,
        "updated_at": stored.updated_at.isoformat() if stored.updated_at else None,
        "index_status": stored.index.status if stored.index else "none",
    }


TOOL_HANDLERS = {
    "search_files": lambda user, args, drive: _tool_search_files(
        user, args.get("query", ""), drive),
    "read_file": lambda user, args, drive: _tool_read_file(
        user, args.get("file_id"), args.get("start", 0), args.get("length", 20_000)),
    "list_files": lambda user, args, drive: _tool_list_files(
        user, args.get("folder_id"), drive),
    "get_file_info": lambda user, args, drive: _tool_get_file_info(
        user, args.get("file_id")),
}

_STEP_LABELS = {
    "search_files": "Searched files",
    "read_file": "Read file",
    "list_files": "Listed drive",
    "get_file_info": "Checked file info",
}


def _summarize_result(name, result):
    """One-line human summary of a tool result, shown live in the chat UI."""
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"
    if name == "search_files":
        if not result:
            return "No files found."
        names = ", ".join(r["name"] for r in result[:5])
        more = f" (+{len(result) - 5} more)" if len(result) > 5 else ""
        return f"Found {len(result)} file(s): {names}{more}"
    if name == "read_file":
        base = (f"Read {result.get('returned_chars', 0):,} of "
                f"{result.get('total_chars', 0):,} chars")
        return base + (" — more content available" if result.get("has_more") else " — end of file")
    if name == "list_files":
        return (f"{len(result.get('folders', []))} folder(s), "
                f"{len(result.get('files', []))} file(s)")
    if name == "get_file_info":
        return (f"{result.get('name', '?')} — {result.get('size', 0):,} bytes, "
                f"index: {result.get('index_status', '?')}")
    return str(result)[:200]


def run_agent_events(user, history, drive=None):
    """Run the multi-step tool-calling loop, yielding events as they happen.

    `history` is a list of {"role": ..., "content": ...} chat messages.
    `drive` optionally scopes search/list tools to a single drive.
    Yields, in order:
      ("thinking", text)                       — the model's intermediate reasoning
      ("step", {"label":..., "detail":...})    — a tool call being made
      ("tool_result", {"label":..., "summary":...}) — what the tool returned
      ("answer", text)                         — the final answer (always last)
    """
    config = ai_service.config_for(user)
    max_steps = config.get("max_steps") or current_app.config.get("AI_MAX_STEPS", 16)
    system = SYSTEM_PROMPT
    if drive is not None:
        system += (f"\n- You are currently working inside the user's "
                   f'"{drive.name}" drive — only files from that drive are '
                   f"visible to you.")
    # Merge any leading system messages from the caller (e.g. the attachments
    # notice added by the chat route) into the main system prompt — some chat
    # templates (e.g. Qwen in LM Studio) reject a system message that is not
    # the first and only one.
    history = list(history)
    while history and history[0].get("role") == "system":
        system += "\n" + history.pop(0)["content"]
    messages = [{"role": "system", "content": system}] + history
    nudges = 0

    for _ in range(max_steps):
        message = ai_service.chat_completion(messages, tools=TOOLS, config=config)
        messages.append(message)

        # Intermediate reasoning: either plain content alongside tool calls,
        # or a dedicated reasoning field (Kimi/DeepSeek-style thinking models)
        reasoning = (message.get("content") or "").strip()
        if not reasoning:
            reasoning = (message.get("reasoning_content")
                         or message.get("reasoning") or "").strip()
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            if reasoning and nudges < _MAX_NUDGES and _INTENT_RE.search(reasoning):
                # The model narrated its next step without calling the tool —
                # surface it as reasoning and push the model to continue.
                nudges += 1
                yield ("thinking", reasoning)
                messages.append({"role": "user", "content": _NUDGE})
                continue
            yield ("answer", reasoning)
            return

        if reasoning:
            yield ("thinking", reasoning)

        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            raw_args = call.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}

            handler = TOOL_HANDLERS.get(name)
            result = handler(user, args, drive) if handler else {"error": f"Unknown tool '{name}'."}

            detail = args.get("query") or result.get("name") or args.get("file_id") or ""
            if name == "read_file" and args.get("start"):
                detail = f"{detail} (from char {args['start']})"
            label = _STEP_LABELS.get(name, name)
            yield ("step", {"label": label, "detail": str(detail)})
            yield ("tool_result", {"label": label,
                                   "summary": _summarize_result(name, result)})

            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    # Out of steps: force a final answer (no tools offered) with whatever was
    # gathered, so the user gets a usable — possibly partial — answer and can
    # continue the conversation instead of hitting a dead end.
    messages.append({"role": "user", "content": (
        "You have used all available tool steps. Write the final answer now "
        "with the information you already have. If something is still "
        "missing, say so honestly and summarize what you found.")})
    try:
        final = ai_service.chat_completion(messages, config=config)
        answer = (final.get("content") or "").strip()
    except ai_service.AIError:
        answer = ""
    yield ("answer",
           answer or "I reached the maximum number of search steps without a "
                      "final answer. Please try rephrasing your question.")


def run_agent(user, history, drive=None):
    """Non-streaming wrapper: returns (answer, steps)."""
    answer, steps = "", []
    for kind, payload in run_agent_events(user, history, drive=drive):
        if kind == "step":
            steps.append(payload)
        else:
            answer = payload
    return answer, steps
