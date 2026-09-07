import json
import re

from flask import current_app
from sqlalchemy import func

from ..extensions import db
from ..models import StoredFile
from . import ai_service, file_service, hashtag_service, search_service

_SEARCH_FILTERS_SCHEMA = {
    "extensions": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Only these extensions, e.g. [\"pdf\", \"md\"].",
    },
    "folder_id": {
        "type": "integer",
        "description": "Restrict to this folder.",
    },
    "recursive": {
        "type": "boolean",
        "description": "With folder_id, also include subfolders (default false).",
    },
    "min_words": {
        "type": "integer",
        "description": "Only files with at least this many words.",
    },
    "max_words": {
        "type": "integer",
        "description": "Only files with at most this many words.",
    },
    "min_size": {
        "type": "integer",
        "description": "Only files at least this many bytes.",
    },
    "max_size": {
        "type": "integer",
        "description": "Only files at most this many bytes.",
    },
    "has_tags": {
        "type": "boolean",
        "description": "Only files that have hashtags.",
    },
    "has_caption": {
        "type": "boolean",
        "description": "Only files with an AI image caption.",
    },
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Full-text search over the user's files (filenames, hashtags, "
                "extracted document text and AI image captions), powered by "
                "FTS5. Supports \"quoted phrases\", boolean operators "
                "(AND, OR, NOT) and column scopes (name:, tags:, caption:, "
                "text:). Plain words are matched by prefix — append '*' to a "
                "word stem to also match longer forms (e.g. budg* matches "
                "budget and budgets). Returns matching files with snippets, "
                "plus 'total' (how many files matched overall). Narrow big "
                "result sets with the metadata filters, and use count_files "
                "first to measure how many files a query hits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "FTS5 query: plain terms (prefix-matched, ANDed), "
                            "\"quoted phrases\", AND/OR/NOT, or column scopes "
                            "like tags:finance."),
                    },
                    **_SEARCH_FILTERS_SCHEMA,
                    "sort": {
                        "type": "string",
                        "enum": ["relevance", "name", "size", "word_count",
                                 "updated_at"],
                        "description": "Result order (default relevance).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10, max 25).",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many results, for pagination (default 0).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_files",
            "description": (
                "Count how many files match a query and/or metadata filters, "
                "with breakdowns by extension and drive — WITHOUT reading any "
                "content. Use this to measure a search before diving in: if "
                "the total is huge, narrow the query or filters; if zero, "
                "broaden them. Accepts the same query and filters as "
                "search_files (except sort/limit/offset)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Same query syntax as search_files; omit to count with filters only.",
                    },
                    **_SEARCH_FILTERS_SCHEMA,
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the extracted text or caption of a file by its id. Large "
                "files are returned in chunks: use 'start' (character offset) "
                "and 'length' to page through them, and check 'has_more' and "
                "'total_chars' in the result to know if you should continue "
                "reading. For large files, prefer locating the relevant part "
                "with grep_file first and then reading only that region."
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
            "name": "grep_file",
            "description": (
                "Search for a word or phrase INSIDE one file's extracted text "
                "and return only the matching lines (numbered) with "
                "surrounding context — much cheaper than read_file for large "
                "files. Use it to locate the exact section that matters, then "
                "read_file around that spot only if more context is needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "integer", "description": "The file id."},
                    "pattern": {
                        "type": "string",
                        "description": "Text to find (case-insensitive literal match).",
                    },
                    "context": {
                        "type": "integer",
                        "description": "Lines of context around each match (default 2, max 10).",
                    },
                },
                "required": ["file_id", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List the files and folders in the user's drive, optionally "
                "inside one folder. With recursive=true, lists every folder "
                "(with parent ids and file counts) and every file (up to 200) "
                "in the whole drive — a cheap map of the drive's structure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_id": {
                        "type": "integer",
                        "description": "Folder id to list; omit for the root.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "List the whole drive tree instead of one level (default false).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_info",
            "description": (
                "Get metadata for a file by its id: name, size, type, dates, "
                "word/line counts, folder path, hashtag list, caption "
                "presence and number of stored versions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "integer", "description": "The file id."},
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_hashtags",
            "description": (
                "Save search hashtags for a file, replacing the current ones. "
                "Only use this when the user explicitly asks you to create or "
                "change a file's hashtags. Each hashtag must be short and "
                "lowercase, with no '#' prefix."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "integer", "description": "The file id."},
                    "hashtags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The hashtags to save (max 10).",
                    },
                },
                "required": ["file_id", "hashtags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_hashtags",
            "description": (
                "List every hashtag used across the user's files, with usage "
                "counts. Use this to discover the existing tag vocabulary, then "
                "search_files with the exact tag terms (or tags:<term>) for "
                "faster, precise results."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
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
- Search like a funnel: broad first, then narrow. When a topic may span many
  files, call count_files first to measure the result set; if it is large,
  refine with a better query or filters (extensions, min_words, folder)
  before calling search_files.
- search_files uses FTS5 syntax: "quoted phrases", AND/OR/NOT and column
  scopes (name:, tags:, caption:). Plain words match by prefix, so use word
  stems with '*' (e.g. budg* matches budget/budgets).
- To answer a question about a specific file, call grep_file first to find
  the exact lines that matter, then read_file only around that section if
  you need more context. Never read a whole large file when grep_file can
  pinpoint the relevant part.
- Prefer a single tool call per step; wait for the result before continuing.
- Large files are read in chunks: when read_file returns has_more=true, call
  it again with start set to start + returned_chars to continue reading.
  Continue until you have the information you need.
- Always cite the files you used by name, like [filename](file://ID).
- When the user asks you to create hashtags for a file, read the file first,
  then call set_hashtags with up to 10 short, lowercase tags (no '#' prefix).
- Files can have hashtags, and search_files matches them with a high score.
  When the user mentions a tag or topic, call list_hashtags first to find the
  exact tag terms in use, then search_files with those terms.
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

# grep_file output budgets: the goal is to point the model at the right
# section, not to stream the whole file through the chat.
GREP_MAX_MATCHES = 40
GREP_MAX_CHARS = 8000

# Context-window budget: accumulated tool results are what blows up the
# prompt (a handful of 50k read_file chunks already exceeds most local
# models). Only the most recent tool results are kept intact; older ones
# are trimmed to a head + a note telling the model to re-call the tool.
_TOOL_HISTORY_KEEP = 4
_TOOL_RESULT_TRIM = 1000


def _trim_tool_messages(messages):
    """Trim older tool results in-place so the prompt stays within the
    model's context window. The last `_TOOL_HISTORY_KEEP` tool messages are
    kept intact; older ones longer than `_TOOL_RESULT_TRIM` chars are cut
    to their head (which keeps file ids/names) plus a re-call hint."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-_TOOL_HISTORY_KEEP] if len(tool_idx) > _TOOL_HISTORY_KEEP else []:
        content = messages[i].get("content") or ""
        if len(content) > _TOOL_RESULT_TRIM:
            messages[i] = {**messages[i], "content": (
                content[:_TOOL_RESULT_TRIM]
                + f"… [trimmed {len(content) - _TOOL_RESULT_TRIM} chars to fit the "
                  "context window — call the tool again if you need this data]")}


def _tool_search_files(user, query, drive=None, **kw):
    out = search_service.search_advanced(
        user, query,
        sort=kw.get("sort") or "relevance",
        offset=kw.get("offset") or 0,
        limit=kw.get("limit") or 10,
        drive=drive,
        extensions=kw.get("extensions"),
        folder_id=kw.get("folder_id"),
        recursive=bool(kw.get("recursive")),
        min_words=kw.get("min_words"),
        max_words=kw.get("max_words"),
        min_size=kw.get("min_size"),
        max_size=kw.get("max_size"),
        has_tags=bool(kw.get("has_tags")),
        has_caption=bool(kw.get("has_caption")),
    )
    # Keep the JSON payload compact: drop empty/None fields.
    results = [
        {k: v for k, v in r.items() if v not in (None, "", [])}
        for r in out["results"]
    ]
    return {"results": results, "total": out["total"]}


def _tool_count_files(user, query=None, drive=None, **kw):
    return search_service.count_files(
        user, query,
        drive=drive,
        extensions=kw.get("extensions"),
        folder_id=kw.get("folder_id"),
        recursive=bool(kw.get("recursive")),
        min_words=kw.get("min_words"),
        max_words=kw.get("max_words"),
        min_size=kw.get("min_size"),
        max_size=kw.get("max_size"),
        has_tags=bool(kw.get("has_tags")),
        has_caption=bool(kw.get("has_caption")),
    )


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


def _tool_grep_file(user, file_id, pattern, context=2):
    stored = (StoredFile.query
              .filter_by(id=file_id, user_id=user.id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if not stored:
        return {"error": "File not found."}
    pattern = (pattern or "").strip()
    if not pattern:
        return {"error": "Empty pattern."}

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
        return {"file_id": stored.id, "name": stored.name, "matches": 0,
                "note": "No text content available for this file."}

    lines = content.splitlines()
    needle = pattern.lower()
    hits = [i for i, ln in enumerate(lines) if needle in ln.lower()]
    if not hits:
        return {"file_id": stored.id, "name": stored.name, "matches": 0,
                "shown": 0, "truncated": False, "content": ""}

    ctx = max(0, min(int(context or 0) if context else 2, 10))
    # Merge overlapping context windows so no line is emitted twice.
    windows = []
    for i in hits[:GREP_MAX_MATCHES]:
        lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
        if windows and lo <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
        else:
            windows.append((lo, hi))

    out, used = [], 0
    char_truncated = False
    for lo, hi in windows:
        if out:
            out.append("…")
        for n in range(lo, hi):
            line = f"{n + 1}: {lines[n]}"
            if used + len(line) > GREP_MAX_CHARS:
                char_truncated = True
                break
            out.append(line)
            used += len(line) + 1
        if char_truncated:
            break

    return {
        "file_id": stored.id,
        "name": stored.name,
        "matches": len(hits),
        "shown": min(len(hits), GREP_MAX_MATCHES),
        "truncated": len(hits) > GREP_MAX_MATCHES or char_truncated,
        "content": "\n".join(out),
    }


def _tool_list_files(user, folder_id=None, drive=None, recursive=False):
    from ..models import Folder

    files_q = (StoredFile.query
               .filter_by(user_id=user.id)
               .filter(StoredFile.deleted_at.is_(None)))
    folders_q = Folder.query.filter_by(user_id=user.id)
    if drive is not None:
        files_q = files_q.filter(StoredFile.drive_id == drive.id)
        folders_q = folders_q.filter(Folder.drive_id == drive.id)

    if recursive:
        counts = dict(
            files_q.with_entities(StoredFile.folder_id, func.count())
            .group_by(StoredFile.folder_id).all())
        files = files_q.order_by(StoredFile.name).limit(201).all()
        return {
            "folders": [
                {"folder_id": f.id, "name": f.name, "parent_id": f.parent_id,
                 "file_count": counts.get(f.id, 0)}
                for f in folders_q.order_by(Folder.name).all()
            ],
            "files": [
                {"file_id": f.id, "name": f.name, "folder_id": f.folder_id,
                 "size": f.size, "read_only": f.is_synced}
                for f in files[:200]
            ],
            "truncated": len(files) > 200,
        }

    if folder_id:
        files_q = files_q.filter_by(folder_id=folder_id)
        folders_q = folders_q.filter_by(parent_id=folder_id)
    else:
        files_q = files_q.filter(StoredFile.folder_id.is_(None))
        folders_q = folders_q.filter(Folder.parent_id.is_(None))
    return {
        "folders": [{"folder_id": f.id, "name": f.name} for f in folders_q.all()],
        "files": [
            {"file_id": f.id, "name": f.name, "size": f.size,
             "read_only": f.is_synced}
            for f in files_q.limit(100).all()
        ],
    }


def _tool_get_file_info(user, file_id):
    from ..models import Folder

    stored = (StoredFile.query
              .filter_by(id=file_id, user_id=user.id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if not stored:
        return {"error": "File not found."}

    # Walk up the folder tree to build the full path (bounded, cycle-safe).
    parts, folder, seen = [], stored.folder, set()
    while folder is not None and folder.id not in seen and len(parts) < 20:
        seen.add(folder.id)
        parts.append(folder.name)
        folder = (Folder.query.get(folder.parent_id)
                  if folder.parent_id else None)
    folder_path = "/".join(reversed(parts)) or "/"

    index = stored.index
    return {
        "file_id": stored.id,
        "name": stored.name,
        "extension": stored.extension,
        "size": stored.size,
        "folder_path": folder_path,
        "created_at": stored.created_at.isoformat() if stored.created_at else None,
        "updated_at": stored.updated_at.isoformat() if stored.updated_at else None,
        "index_status": index.status if index else "none",
        "word_count": index.word_count if index else None,
        "line_count": index.line_count if index else None,
        "has_caption": bool(index and index.caption),
        "versions": len(stored.versions),
        "hashtags": hashtag_service.get_tags(index),
    }


def _tool_set_hashtags(user, file_id, hashtags):
    stored = (StoredFile.query
              .filter_by(id=file_id, user_id=user.id)
              .filter(StoredFile.deleted_at.is_(None))
              .first())
    if not stored:
        return {"error": "File not found."}
    tags = hashtag_service.set_tags(stored, hashtags, source="ai")
    return {"file_id": stored.id, "name": stored.name, "hashtags": tags}


def _tool_list_hashtags(user, drive=None):
    tags = hashtag_service.get_all_tags(user, drive)
    return [{"tag": tag, "count": count} for tag, count in tags[:50]]


TOOL_HANDLERS = {
    "search_files": lambda user, args, drive: _tool_search_files(
        user, args.get("query", ""), drive,
        **{k: v for k, v in args.items() if k != "query"}),
    "count_files": lambda user, args, drive: _tool_count_files(
        user, args.get("query"), drive,
        **{k: v for k, v in args.items() if k != "query"}),
    "read_file": lambda user, args, drive: _tool_read_file(
        user, args.get("file_id"), args.get("start", 0), args.get("length", 20_000)),
    "grep_file": lambda user, args, drive: _tool_grep_file(
        user, args.get("file_id"), args.get("pattern"), args.get("context", 2)),
    "list_files": lambda user, args, drive: _tool_list_files(
        user, args.get("folder_id"), drive, bool(args.get("recursive"))),
    "get_file_info": lambda user, args, drive: _tool_get_file_info(
        user, args.get("file_id")),
    "set_hashtags": lambda user, args, drive: _tool_set_hashtags(
        user, args.get("file_id"), args.get("hashtags") or []),
    "list_hashtags": lambda user, args, drive: _tool_list_hashtags(user, drive),
}

_STEP_LABELS = {
    "search_files": "Searched files",
    "count_files": "Counted files",
    "read_file": "Read file",
    "grep_file": "Searched inside file",
    "list_files": "Listed drive",
    "get_file_info": "Checked file info",
    "set_hashtags": "Saved hashtags",
    "list_hashtags": "Listed hashtags",
}


def _summarize_result(name, result):
    """One-line human summary of a tool result, shown live in the chat UI."""
    if isinstance(result, dict) and "error" in result:
        return f"Error: {result['error']}"
    if name == "search_files":
        found = result.get("results", [])
        total = result.get("total", len(found))
        if not found:
            return "No files found."
        names = ", ".join(r["name"] for r in found[:5])
        more = f" (+{total - 5} more)" if total > 5 else ""
        return f"Found {total} file(s): {names}{more}"
    if name == "count_files":
        total = result.get("total", 0)
        exts = result.get("by_extension") or {}
        top = ", ".join(f".{e or '?'}×{n}" for e, n in
                        sorted(exts.items(), key=lambda kv: -kv[1])[:4])
        return f"{total} file(s)" + (f" — {top}" if top else "")
    if name == "read_file":
        base = (f"Read {result.get('returned_chars', 0):,} of "
                f"{result.get('total_chars', 0):,} chars")
        return base + (" — more content available" if result.get("has_more") else " — end of file")
    if name == "grep_file":
        matches = result.get("matches", 0)
        if not matches:
            return "No matches inside the file."
        extra = " (truncated)" if result.get("truncated") else ""
        return f"{matches} matching line(s) in {result.get('name', '?')}{extra}"
    if name == "list_files":
        return (f"{len(result.get('folders', []))} folder(s), "
                f"{len(result.get('files', []))} file(s)")
    if name == "get_file_info":
        return (f"{result.get('name', '?')} — {result.get('size', 0):,} bytes, "
                f"index: {result.get('index_status', '?')}")
    if name == "list_hashtags":
        if not result:
            return "No hashtags in use yet."
        tags = ", ".join(f"#{r['tag']} ({r['count']})" for r in result[:10])
        more = f" (+{len(result) - 10} more)" if len(result) > 10 else ""
        return f"{len(result)} hashtag(s): {tags}{more}"
    return str(result)[:200]


def _complete(messages, tools, config):
    """One model call, streaming when enabled.

    Yields ("thinking_token", text) for reasoning deltas and
    ("answer_token", text) for content deltas; returns the assembled message
    dict (same shape as chat_completion's). Falls back once to the
    non-streaming chat_completion if the provider fails before sending any
    delta (e.g. it rejects streamed tool calls).
    """
    if not config.get("streaming", True):
        return ai_service.chat_completion(messages, tools=tools, config=config)

    got_delta = False
    try:
        stream = ai_service.chat_completion_stream(
            messages, tools=tools, config=config)
        while True:
            try:
                kind, payload = next(stream)
            except StopIteration:
                break
            if kind == "done":
                return payload
            got_delta = True
            # Expose deltas to the caller as chat events.
            yield ("thinking_token" if kind == "reasoning" else "answer_token",
                   payload)
    except ai_service.AIError:
        if got_delta:
            raise  # mid-stream failure: the route reports it as an error event
        current_app.logger.warning(
            "AI streaming failed before any delta; retrying non-streaming.")
        return ai_service.chat_completion(messages, tools=tools, config=config)
    raise ai_service.AIError("AI stream ended without a completed message.")


def run_agent_events(user, history, drive=None):
    """Run the multi-step tool-calling loop, yielding events as they happen.

    `history` is a list of {"role": ..., "content": ...} chat messages.
    `drive` optionally scopes search/list tools to a single drive.
    Yields, in order:
      ("thinking_token", text)               — live reasoning delta (streaming)
      ("answer_token", text)                 — live content delta (streaming)
      ("thinking", text)                       — the model's intermediate reasoning
      ("step", {"label":..., "detail":...})    — a tool call being made
      ("tool_result", {"label":..., "summary":...}) — what the tool returned
      ("answer", text)                         — the final answer (always last)

    With streaming, a ("thinking", text) event that follows answer_token
    events carries the same text the client already rendered in a tentative
    answer bubble — the client should move that bubble into the reasoning box
    (the message turned out to be narration before a tool call, not the final
    answer).
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
        _trim_tool_messages(messages)
        message = yield from _complete(messages, TOOLS, config)
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

            detail = args.get("query") or args.get("pattern") \
                or (result.get("name") if isinstance(result, dict) else "") \
                or args.get("file_id") or ""
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
    answer = ""
    try:
        final = yield from _complete(messages, None, config)
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
        elif kind in ("thinking", "answer"):
            answer = payload
        # thinking_token / answer_token deltas are ignored here — the final
        # "answer" event always carries the complete text.
    return answer, steps
