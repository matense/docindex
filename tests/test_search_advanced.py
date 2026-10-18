import io
from unittest.mock import patch

from app.extensions import db
from app.models import StoredFile, User
from app.services import embedding_service, hashtag_service, search_service


def _upload(client, name, content):
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def _user(app, user_id):
    return db.session.get(User, user_id)


def _set_caption(name, caption):
    index = StoredFile.query.filter_by(name=name).one().index
    index.caption = caption
    db.session.commit()


# ------------------------------------------------------------------- syntax


def test_phrase_search_matches_adjacency_only(auth_client, app, user):
    _upload(auth_client, "a.txt", b"the quarterly budget review happened today")
    _upload(auth_client, "b.txt", b"budget items are discussed every quarterly meeting")
    with app.app_context():
        out = search_service.search_advanced(_user(app, user), '"quarterly budget"')
        assert out["total"] == 1
        assert out["results"][0]["name"] == "a.txt"


def test_boolean_not_excludes_matches(auth_client, app, user):
    _upload(auth_client, "a.txt", b"the budget report is ready")
    _upload(auth_client, "b.txt", b"the invoice and budget arrived")
    with app.app_context():
        out = search_service.search_advanced(_user(app, user), "budget NOT invoice")
        assert out["total"] == 1
        assert out["results"][0]["name"] == "a.txt"

        out = search_service.search_advanced(_user(app, user), "invoice OR report")
        assert out["total"] == 2


def test_column_scope_tags(auth_client, app, user):
    _upload(auth_client, "tagged.txt", b"completely unrelated body")
    _upload(auth_client, "plain.txt", b"this one mentions finance in the text")
    with app.app_context():
        hashtag_service.set_tags(
            StoredFile.query.filter_by(name="tagged.txt").one(),
            ["finance"], source="user")
        search_service.fts_upsert(
            StoredFile.query.filter_by(name="tagged.txt").one().id)
        u = _user(app, user)
        scoped = search_service.search_advanced(u, "tags:finance")
        assert scoped["total"] == 1
        assert scoped["results"][0]["name"] == "tagged.txt"
        # Unscoped, both files match the term.
        assert search_service.search_advanced(u, "finance")["total"] == 2


def test_plain_query_prefix_matches(auth_client, app, user):
    _upload(auth_client, "a.txt", b"the budget is approved")
    with app.app_context():
        # Plain queries keep prefix semantics: "budg" matches "budget".
        assert search_service.search_advanced(_user(app, user), "budg")["total"] == 1


def test_invalid_fts_syntax_falls_back_without_crashing(auth_client, app, user):
    _upload(auth_client, "a.txt", b"the budget is approved")
    with app.app_context():
        out = search_service.search_advanced(_user(app, user), "budget AND (broken")
        assert isinstance(out["total"], int)  # no crash; honest empty result
        assert out["results"] == []


# ------------------------------------------------------------------ filters


def test_filter_by_extension(auth_client, app, user):
    _upload(auth_client, "notes.txt", b"shared content here")
    _upload(auth_client, "notes.md", b"shared content here")
    with app.app_context():
        u = _user(app, user)
        out = search_service.search_advanced(u, "shared", extensions=["md"])
        assert out["total"] == 1
        assert out["results"][0]["name"] == "notes.md"


def test_filter_by_word_count(auth_client, app, user):
    _upload(auth_client, "short.txt", b"one two")
    _upload(auth_client, "long.txt", b"one two three four five six seven eight")
    with app.app_context():
        u = _user(app, user)
        assert {r["name"] for r in search_service.search_advanced(
            u, "one", min_words=5)["results"]} == {"long.txt"}
        assert {r["name"] for r in search_service.search_advanced(
            u, "one", max_words=3)["results"]} == {"short.txt"}


def test_filter_has_tags_and_has_caption(auth_client, app, user):
    _upload(auth_client, "tagged.txt", b"common body")
    _upload(auth_client, "captioned.txt", b"common body")
    _upload(auth_client, "bare.txt", b"common body")
    with app.app_context():
        hashtag_service.set_tags(
            StoredFile.query.filter_by(name="tagged.txt").one(),
            ["alpha"], source="user")
        _set_caption("captioned.txt", "a nice caption")
        u = _user(app, user)
        assert {r["name"] for r in search_service.search_advanced(
            u, "common", has_tags=True)["results"]} == {"tagged.txt"}
        assert {r["name"] for r in search_service.search_advanced(
            u, "common", has_caption=True)["results"]} == {"captioned.txt"}
        # Filters combine.
        both = search_service.search_advanced(
            u, "common", extensions=["txt"], has_tags=True, min_words=1)
        assert {r["name"] for r in both["results"]} == {"tagged.txt"}


# ---------------------------------------------------------- sort/pagination


def test_sort_and_pagination(auth_client, app, user):
    _upload(auth_client, "c.txt", b"needle one two three four")
    _upload(auth_client, "a.txt", b"needle one")
    _upload(auth_client, "b.txt", b"needle one two")
    with app.app_context():
        u = _user(app, user)
        by_name = search_service.search_advanced(u, "needle", sort="name")
        assert [r["name"] for r in by_name["results"]] == ["a.txt", "b.txt", "c.txt"]
        by_words = search_service.search_advanced(u, "needle", sort="word_count")
        assert [r["name"] for r in by_words["results"]] == ["c.txt", "b.txt", "a.txt"]

        page1 = search_service.search_advanced(u, "needle", sort="name", limit=2)
        page2 = search_service.search_advanced(
            u, "needle", sort="name", limit=2, offset=2)
        assert page1["total"] == 3 and page2["total"] == 3
        assert [r["name"] for r in page1["results"]] == ["a.txt", "b.txt"]
        assert [r["name"] for r in page2["results"]] == ["c.txt"]


def test_snippet_has_bracket_markers_not_html(auth_client, app, user):
    _upload(auth_client, "a.txt", b"some leading words then the needle appears here")
    with app.app_context():
        out = search_service.search_advanced(_user(app, user), "needle")
        snip = out["results"][0]["snippet"]
        assert "<mark>" not in snip
        assert "[needle]" in snip.lower()


# --------------------------------------------------------------------- count


def test_count_files_breakdowns(auth_client, app, user):
    _upload(auth_client, "one.txt", b"shared body")
    _upload(auth_client, "two.txt", b"shared body")
    _upload(auth_client, "three.md", b"shared body")
    _upload(auth_client, "other.txt", b"different entirely")
    with app.app_context():
        u = _user(app, user)
        out = search_service.count_files(u, "shared")
        assert out["total"] == 3
        assert out["by_extension"] == {"txt": 2, "md": 1}
        assert sum(out["by_drive"].values()) == 3

        # Filters-only count (no query).
        assert search_service.count_files(u, extensions=["md"])["total"] == 1
        assert search_service.count_files(u, "nomatchanywhere")["total"] == 0


# ---------------------------------------------------------- semantic -----


def _enable_embeddings(app):
    app.config["AI_ENABLED"] = True
    app.config["AI_EMBEDDING_MODEL"] = "test-model"


def _fake_embed(texts, user=None):
    """Deterministic 2-D vectors: cat-related text vs everything else."""
    return [[1.0, 0.0] if "cat" in t.lower() else [0.0, 1.0]
            for t in texts]


def test_chunk_text_splits_and_rejoins():
    text = ("First paragraph here.\n\nSecond paragraph there.\n\n" + "word " * 900)
    chunks = embedding_service.chunk_text(text, size=500, overlap=50)
    assert len(chunks) >= 2
    assert all(len(c) <= 600 for c in chunks)
    # Nothing lost: every word survives in at least one chunk.
    words = text.split()
    assert all(any(w in c for c in chunks) for w in words)


def test_semantic_search_ranks_by_meaning(auth_client, app, user):
    _enable_embeddings(app)
    _upload(auth_client, "cat.txt", b"the cat sits on the mat")
    _upload(auth_client, "dog.txt", b"a dog barks loudly outside")
    with app.app_context():
        u = _user(app, user)
        with patch.object(embedding_service, "embed_texts",
                          side_effect=_fake_embed):
            for stored in StoredFile.query.filter_by(user_id=user).all():
                embedding_service.embed_file(stored, u)
            ranked = search_service.semantic_search_files(u, "cat")
    assert [s.name for s, _, _ in ranked] == ["cat.txt", "dog.txt"]
    assert ranked[0][1] > ranked[1][1]


def test_semantic_falls_back_without_model(auth_client, app, user):
    _upload(auth_client, "budget.txt", b"the budget is approved")
    with app.app_context():
        u = _user(app, user)
        # No embedding model: semantic is empty, auto degrades to keyword.
        assert search_service.search_advanced(
            u, "budget", mode="semantic")["total"] == 0
        out = search_service.search_advanced(u, "budget", mode="auto")
        assert out["total"] == 1
        assert out["results"][0]["name"] == "budget.txt"


def test_hybrid_fuses_keyword_and_semantic(auth_client, app, user):
    _enable_embeddings(app)
    _upload(auth_client, "exact.txt", b"budget budget budget numbers")
    _upload(auth_client, "meaning.txt", b"completely unrelated wording here")
    with app.app_context():
        u = _user(app, user)
        meaning_id = StoredFile.query.filter_by(name="meaning.txt").one().id
        with patch.object(embedding_service, "semantic_search",
                          return_value=[(meaning_id, 0.95, "snippet")]):
            out = search_service.search_advanced(u, "budget", mode="hybrid")
    names = [r["name"] for r in out["results"]]
    assert set(names) == {"exact.txt", "meaning.txt"}
    assert out["total"] == 2


def test_tool_search_forwards_mode(app, user):
    from app.services import agent_service
    seen = {}

    def fake_search(user_, query, **kw):
        seen.update(kw)
        return {"results": [], "total": 0}

    with app.app_context():
        u = db.session.get(User, user)
        with patch.object(search_service, "search_advanced",
                          side_effect=fake_search):
            agent_service._tool_search_files(u, "q", None, mode="semantic")
    assert seen.get("mode") == "semantic"


def test_similar_files_endpoint(auth_client, app, user):
    _enable_embeddings(app)
    _upload(auth_client, "kitten.txt", b"a small cat sleeps")
    _upload(auth_client, "tomcat.txt", b"the big cat hunts")
    _upload(auth_client, "truck.txt", b"a heavy truck engine")
    with patch.object(embedding_service, "embed_texts",
                      side_effect=_fake_embed):
        with app.app_context():
            u = _user(app, user)
            for stored in StoredFile.query.filter_by(user_id=user).all():
                embedding_service.embed_file(stored, u)
            kitten = StoredFile.query.filter_by(name="kitten.txt").one()
            first_id = kitten.id
    resp = auth_client.get(f"/file/{first_id}/similar")
    assert resp.status_code == 200
    names = [f["name"] for f in resp.get_json()["files"]]
    assert "tomcat.txt" in names
    assert "kitten.txt" not in names
