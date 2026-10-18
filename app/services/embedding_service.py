"""Semantic search via embeddings (native, no external vector DB).

The embedding model runs on the user's own OpenAI-compatible AI endpoint
(LM Studio `text-embedding-*`, Ollama `nomic-embed-text`, OpenAI, ...):
``AI_EMBEDDING_MODEL`` names it, the active AI connection provides the
base URL / API key. Empty model = semantic search disabled, everything
falls back to keyword (FTS5) search.

Storage is a plain ``file_embeddings`` table (float32 BLOBs + cosine in
Python, numpy fast path when installed) — no native sqlite-vec extension,
which is fragile on local Windows installs.
"""

import hashlib
import struct

from flask import current_app

from ..extensions import db
from ..models import FileEmbedding, StoredFile
from . import ai_service

try:
    import numpy as _np
    _HAS_NUMPY = True
except ImportError:  # pure-python fallback (slower, zero new deps)
    _HAS_NUMPY = False

EMBED_BATCH = 32
# Bodies the embedding endpoint may reject outright.
MAX_INPUT_CHARS = 8000


def _model_for(user):
    """Embedding model for this user: per-connection value, else the
    global AI_EMBEDDING_MODEL."""
    return (ai_service.config_for(user).get("embedding_model") or "").strip()


def embeddings_available(user=None):
    """True when a query can be embedded: AI on + endpoint + model set."""
    cfg = ai_service.config_for(user)
    return bool(cfg["enabled"] and cfg["base_url"]
                and (cfg.get("embedding_model") or "").strip())


def _headers(cfg):
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    return headers


def embed_texts(texts, user=None):
    """Embed a batch of texts -> list of L2-normalized float lists.

    Raises ai_service.AIError on transport/model errors (callers decide
    whether to fail loud or degrade to keyword search).
    """
    texts = [(t or "")[:MAX_INPUT_CHARS] for t in texts]
    if not texts:
        return []
    cfg = ai_service.config_for(user)
    model = (cfg.get("embedding_model") or "").strip()
    if not model:
        raise ai_service.AIError(
            "Semantic search is off (no embedding model on this connection).")
    if not (cfg["enabled"] and cfg["base_url"]):
        raise ai_service.AIError("AI is not configured.")
    import requests
    ai_service._rate_slot(cfg, block=True)
    try:
        resp = requests.post(
            f"{cfg['base_url']}/embeddings",
            json={"model": model, "input": texts},
            headers=_headers(cfg),
            timeout=cfg["timeout"],
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise ai_service.AIError(
            f"Cannot reach AI backend at {cfg['base_url']}: {exc}") from exc
    if resp.status_code in (301, 302, 303, 307, 308):
        resp.close()
        raise ai_service.AIError("AI backend redirected the request — refusing to follow (SSRF guard).")
    if resp.status_code == 429:
        raise ai_service.AIError("The provider's rate limit was hit (HTTP 429).")
    if resp.status_code != 200:
        raise ai_service.AIError(f"AI backend returned {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()["data"]
        vectors = [row["embedding"] for row in sorted(data, key=lambda r: r["index"])]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ai_service.AIError(f"Unexpected embeddings response format: {exc}") from exc
    out = []
    for vec in vectors:
        norm = sum(x * x for x in vec) ** 0.5
        out.append([x / norm for x in vec] if norm else [0.0] * len(vec))
    return out


def chunk_text(text, size=None, overlap=None):
    """Split text into word-safe chunks of ~size chars with overlap.

    Paragraph-aware: paragraphs are never torn in half unless a single
    one exceeds the size (then it is hard-split).
    """
    if size is None:
        size = current_app.config.get("AI_EMBED_CHUNK_CHARS", 2000)
    if overlap is None:
        overlap = current_app.config.get("AI_EMBED_CHUNK_OVERLAP", 200)
    size = max(200, int(size))
    overlap = max(0, min(int(overlap), size - 1))
    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    chunks, current = [], ""
    for para in paras:
        while len(para) > size:  # oversized paragraph: hard-split
            chunks.append(para[:size])
            para = para[size - overlap:]
        if len(current) + len(para) + 2 <= size or not current:
            current = (current + "\n\n" + para).strip()
        else:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = (tail + "\n\n" + para).strip() if tail else para
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *[float(x) for x in vec])


def _unpack(blob, dims):
    if not blob or not dims:
        return None
    try:
        return list(struct.unpack(f"{dims}f", bytes(blob)[:dims * 4]))
    except struct.error:
        return None


def cosine(a, b):
    """Cosine similarity of two L2-normalized vectors (0..1-ish)."""
    if _HAS_NUMPY:
        try:
            return float(_np.dot(_np.asarray(a, dtype=_np.float32),
                                _np.asarray(b, dtype=_np.float32)))
        except Exception:  # noqa: BLE001 - fall through to pure python
            pass
    return sum(x * y for x, y in zip(a, b))


def embed_file(stored, user=None):
    """(Re)embed one file's chunks; skips unchanged chunks by text hash.

    Images embed their caption (+name); text-like files embed extracted
    text. Returns the number of chunks stored. Files with nothing
    embeddable keep zero rows (and any stale rows are removed).
    """
    from ..models import User
    if user is None:
        user = db.session.get(User, stored.user_id)
    index = stored.index
    if stored.is_image:
        source = ((index.caption if index and index.caption else "")
                  + "\n" + stored.name).strip()
    else:
        source = (index.extracted_text if index and index.extracted_text else "") or ""
    chunks = chunk_text(source)[:current_app.config.get("AI_EMBED_MAX_CHUNKS", 200)]
    model = _model_for(user)
    if not model:
        return FileEmbedding.query.filter_by(file_id=stored.id).count()

    existing = {e.chunk_index: e for e in
                FileEmbedding.query.filter_by(file_id=stored.id).all()}
    wanted = {}
    for i, text in enumerate(chunks):
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row = existing.get(i)
        if row and row.text_hash == h and row.model == model:
            wanted[i] = row
            continue
        wanted[i] = (text, h)
    # Drop rows that no longer correspond to anything.
    stale = [row for i, row in existing.items()
             if i >= len(chunks) or row.model != model
             or row.text_hash != hashlib.sha256(
                 chunks[i].encode("utf-8")).hexdigest()]
    for row in stale:
        db.session.delete(row)
    fresh = [(i, text, h) for i, (text, h) in
             [(i, v) for i, v in wanted.items() if isinstance(v, tuple)]]
    if fresh:
        vectors = embed_texts([t for _, t, _ in fresh], user=user)
        dims = len(vectors[0]) if vectors else 0
        for (i, text, h), vec in zip(fresh, vectors):
            db.session.add(FileEmbedding(
                file_id=stored.id, chunk_index=i, text=text[:4000],
                text_hash=h, embedding=_pack(vec), model=model, dims=dims))
    db.session.commit()
    return FileEmbedding.query.filter_by(file_id=stored.id).count()


_SIMILAR_SCAN_CAP = 20000


def similar_files(stored, user, limit=8):
    """Files with content most similar to this one (excludes self).

    Compares the file's centroid chunk vector against recent chunks
    (capped scan) — a fast "more like this" without a vector index.
    Returns [(StoredFile, score)] best-first.
    """
    own = []
    for e in FileEmbedding.query.filter_by(file_id=stored.id).all():
        vec = _unpack(e.embedding, e.dims)
        if vec:
            own.append(vec)
    if not own:
        return []
    limit = max(1, min(int(limit or 8), 25))
    n = len(own[0])
    centroid = [sum(v[i] for v in own) / len(own) for i in range(n)]
    norm = sum(x * x for x in centroid) ** 0.5
    if norm:
        centroid = [x / norm for x in centroid]

    q = (FileEmbedding.query
         .join(StoredFile, FileEmbedding.file_id == StoredFile.id)
         .filter(StoredFile.user_id == user.id,
                 StoredFile.deleted_at.is_(None),
                 FileEmbedding.file_id != stored.id)
         .order_by(FileEmbedding.id.desc())
         .limit(_SIMILAR_SCAN_CAP))
    best = {}
    for row in q.yield_per(1000):
        vec = _unpack(row.embedding, row.dims)
        if not vec:
            continue
        s = cosine(centroid, vec)
        if s > best.get(row.file_id, -1):
            best[row.file_id] = s
    top = sorted(best.items(), key=lambda kv: -kv[1])[:limit]
    if not top:
        return []
    files = {f.id: f for f in StoredFile.query.filter(
        StoredFile.id.in_([fid for fid, _ in top])).all()}
    return [(files[fid], score) for fid, score in top if fid in files]


def semantic_search(user, query, limit=50, drive=None, file_ids=None,
                    with_text=False):
    """Nearest file chunks to the query, best-first.

    Returns [(file_id, score)] — or [(file_id, score, chunk_text)] with
    ``with_text=True``. Scores are max-chunk cosine per file.
    ``drive`` scopes like the keyword search (Drive or list); ``file_ids``
    restricts to an explicit set. Raises ai_service.AIError when embeddings
    are unavailable.
    """
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 50), 200))
    qvec = embed_texts([query], user=user)[0]

    q = (FileEmbedding.query.join(
            StoredFile, FileEmbedding.file_id == StoredFile.id)
         .filter(StoredFile.user_id == user.id,
                 StoredFile.deleted_at.is_(None)))
    if drive is not None:
        drives = list(drive) if isinstance(drive, (list, tuple)) else [drive]
        dids = [d.id for d in drives]
        q = q.filter(StoredFile.drive_id.in_(dids))
    if file_ids is not None:
        ids = list(file_ids)
        if not ids:
            return []
        q = q.filter(FileEmbedding.file_id.in_(ids))

    best = {}
    best_text = {}
    batch, batch_rows = [], []
    for row in q.yield_per(1000):
        vec = _unpack(row.embedding, row.dims)
        if not vec:
            continue
        batch.append(vec)
        batch_rows.append((row.file_id, row.text))
        if len(batch) >= 500:
            _score_batch(qvec, batch, batch_rows, best, best_text)
            batch, batch_rows = [], []
    if batch:
        _score_batch(qvec, batch, batch_rows, best, best_text)
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    if with_text:
        return [(fid, score, best_text.get(fid)) for fid, score in ranked[:limit]]
    return ranked[:limit]


def _score_batch(qvec, batch, file_rows, best, best_text=None):
    if _HAS_NUMPY:
        try:
            import numpy as _np2
            m = _np2.asarray(batch, dtype=_np2.float32)
            q = _np2.asarray(qvec, dtype=_np2.float32)
            for (fid, text), s in zip(file_rows, m.dot(q).tolist()):
                if s > best.get(fid, -1):
                    best[fid] = float(s)
                    if best_text is not None:
                        best_text[fid] = text
            return
        except Exception:  # noqa: BLE001 - fall through to pure python
            pass
    for (fid, text), vec in zip(file_rows, batch):
        s = cosine(qvec, vec)
        if s > best.get(fid, -1):
            best[fid] = s
            if best_text is not None:
                best_text[fid] = text
