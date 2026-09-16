import io
from unittest.mock import patch

from app.extensions import db
from app.models import StoredFile, User
from app.services import indexing_service, search_service


def _upload(client, name, content):
    return client.post("/upload", data={
        "files": (io.BytesIO(content), name),
    }, content_type="multipart/form-data", follow_redirects=True)


def test_index_text_file(auth_client, app):
    _upload(auth_client, "report.txt", b"quarterly revenue grew by 42 percent")
    with app.app_context():
        stored = StoredFile.query.one()
        assert stored.index.status == "ok"
        assert "quarterly revenue" in stored.index.extracted_text


def test_search_finds_indexed_text(auth_client, app, user):
    _upload(auth_client, "report.txt", b"quarterly revenue grew by 42 percent")
    with app.app_context():
        user_obj = db.session.get(User, user)
        results = search_service.search_files("revenue", user_obj)
        assert len(results) == 1
        assert results[0]["file"].name == "report.txt"
        assert "<mark>" in results[0]["snippet"]


def test_search_finds_caption(auth_client, app, user):
    _upload(auth_client, "photo.txt", b"nothing relevant")
    with app.app_context():
        stored = StoredFile.query.one()
        stored.index.extracted_text = None
        stored.index.caption = "A golden retriever playing in the snow"
        stored.index.status = "ok"
        db.session.commit()
        # Direct index mutation bypasses the service layer — refresh FTS.
        search_service.fts_upsert(stored.id)

        user_obj = db.session.get(User, user)
        results = search_service.search_files("retriever", user_obj)
        assert len(results) == 1


def test_search_page(auth_client):
    _upload(auth_client, "manual.txt", b"installation instructions for the server rack")
    resp = auth_client.get("/search?q=installation")
    assert b"manual.txt" in resp.data


def test_instant_search_api(auth_client):
    _upload(auth_client, "api-doc.txt", b"authentication endpoint reference")
    resp = auth_client.get("/api/search?q=authentication")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data[0]["name"] == "api-doc.txt"


def test_image_indexing_caption_called(auth_client, app):
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    app.config["AI_ENABLED"] = True
    with patch("app.services.ai_service.caption_image", return_value="a tiny png") as cap, \
         patch("app.services.indexing_service._extract_image_ocr", return_value=""):
        # TestConfig indexes synchronously, so upload triggers indexing inline.
        _upload(auth_client, "pixel.png", png)
        with app.app_context():
            stored = StoredFile.query.one()
            cap.assert_called_once()
            assert stored.index.caption == "a tiny png"
            assert stored.index.status == "ok"


def test_index_error_status(auth_client, app):
    with patch("app.services.indexing_service.extract_text",
               side_effect=RuntimeError("boom")):
        _upload(auth_client, "broken.txt", b"data")
    with app.app_context():
        stored = StoredFile.query.one()
        assert stored.index.status == "error"
        assert "boom" in stored.index.error


def test_reindex_after_edit(auth_client, app):
    _upload(auth_client, "notes.md", b"first version")
    with app.app_context():
        fid = StoredFile.query.one().id
    auth_client.post(f"/file/{fid}/edit", data={"content": "second version with zephyr"},
                     follow_redirects=True)
    with app.app_context():
        stored = db.session.get(StoredFile, fid)
        assert "zephyr" in stored.index.extracted_text


def test_search_page_has_ai_mode_toggle(auth_client):
    resp = auth_client.get("/search")
    assert resp.status_code == 200
    assert b"ai-mode-switch" in resp.data
    assert b"mode=ai" in resp.data


def test_search_ai_mode_renders_chat_page(auth_client):
    resp = auth_client.get("/search?mode=ai")
    assert resp.status_code == 200
    assert b'id="ai-page"' in resp.data
    assert b'id="aip-form"' in resp.data
    assert b"ai_page.js" in resp.data
    assert b"aip-suggestion" in resp.data


def test_search_results_keep_ai_toggle(auth_client):
    resp = auth_client.get("/search?q=anything")
    assert resp.status_code == 200
    assert b"ai-mode-switch" in resp.data


def test_search_page_renders_mark_highlight(auth_client):
    _upload(auth_client, "notes.txt", b"the quarterly revenue report is ready")
    resp = auth_client.get("/search?q=revenue")
    assert resp.status_code == 200
    assert b"<mark>" in resp.data
    assert b"&lt;mark&gt;" not in resp.data


def test_search_snippet_escapes_html(auth_client):
    _upload(auth_client, "evil.txt", b"payload <script>alert(1)</script> end")
    resp = auth_client.get("/search?q=alert")
    assert resp.status_code == 200
    assert b"<script>alert(1)</script>" not in resp.data
    assert b"&lt;script&gt;" in resp.data


def test_search_result_matches_and_name_html(auth_client, app, user):
    _upload(auth_client, "revenue-report.txt", b"nothing about the topic")
    with app.app_context():
        user_obj = db.session.get(User, user)
        results = search_service.search_files("revenue", user_obj)
        assert len(results) == 1
        assert results[0]["matches"] == ["name"]
        assert "<mark>" in results[0]["name_html"]
        assert "revenue" in str(results[0]["name_html"]).lower()


def test_search_result_shows_metadata(auth_client):
    _upload(auth_client, "meta.txt", b"metadata words for the search engine")
    resp = auth_client.get("/search?q=metadata")
    assert resp.status_code == 200
    assert b"badge" in resp.data  # match badge (name/content)
    assert b"words" in resp.data  # word count in the meta row


def _xlsx_bytes():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Region", "Quarter"])
    ws.append(["North", "Q1"])
    ws.append([None, None])  # fully empty rows are skipped
    ws2 = wb.create_sheet("Costs")
    ws2.append(["Item", "Amount"])
    ws2.append(["zebra printer", 340])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_index_xlsx_flattens_all_sheets(auth_client, app):
    _upload(auth_client, "book.xlsx", _xlsx_bytes())
    with app.app_context():
        stored = StoredFile.query.one()
        assert stored.index.status == "ok"
        text = stored.index.extracted_text
        assert "Sheet: Sales" in text and "Sheet: Costs" in text
        assert "Region | Quarter" in text
        assert "North | Q1" in text
        assert "zebra printer | 340" in text


def test_search_and_ai_read_xlsx(auth_client, app, user):
    """Indexed Excel content is searchable and reachable by the AI agent's
    read tool (extracted text path — no dedicated AI tool needed)."""
    from app.services import agent_service
    _upload(auth_client, "book.xlsx", _xlsx_bytes())
    with app.app_context():
        stored = StoredFile.query.one()
        user_obj = db.session.get(User, user)
        results = search_service.search_files("zebra printer", user_obj)
        assert len(results) == 1
        assert results[0]["file"].id == stored.id
        read = agent_service._tool_read_file(user_obj, stored.id)
        assert "Sheet: Sales" in read["content"]
        assert "zebra printer" in read["content"]


def test_extract_xls_uses_xlrd(app):
    """Legacy .xls: xlrd can't write files for a fixture, so mock it and
    verify the dispatch and flattening."""
    import sys
    from unittest.mock import MagicMock
    sheet = MagicMock()
    sheet.name = "Old"
    sheet.get_rows.return_value = iter([["alpha", 1], ["beta", 2]])
    wb = MagicMock()
    wb.sheets.return_value = [sheet]
    fake_xlrd = MagicMock()
    fake_xlrd.open_workbook.return_value = wb
    with patch.dict(sys.modules, {"xlrd": fake_xlrd}):
        text = indexing_service._extract_xls("dummy.xls")
    fake_xlrd.open_workbook.assert_called_once_with("dummy.xls")
    assert "Sheet: Old" in text
    assert "alpha | 1" in text and "beta | 2" in text


def test_corrupt_xlsx_marks_index_error(auth_client, app):
    _upload(auth_client, "broken.xlsx", b"not a zip at all")
    with app.app_context():
        stored = StoredFile.query.one()
        assert stored.index.status == "error"
