import threading

from flask import current_app

from ..extensions import db
from ..models import FileIndex, StoredFile
from . import ai_service, file_service

MAX_TEXT_CHARS = 500_000


def extract_text(stored_file):
    """Extract searchable text from a file based on its extension."""
    ext = stored_file.extension
    path = file_service.file_path(stored_file)

    if ext == "pdf":
        return _extract_pdf(path)
    if ext == "docx":
        return _extract_docx(path)
    if ext in current_app.config["EDITABLE_EXTENSIONS"]:
        return file_service.read_text_content(stored_file, max_chars=MAX_TEXT_CHARS)
    if stored_file.is_image:
        return _extract_image_ocr(path)
    return ""


def _extract_pdf(path):
    import pdfplumber

    parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            parts.append(text)
            for table in page.extract_tables() or []:
                for row in table:
                    parts.append(" | ".join(cell or "" for cell in row))
            if sum(len(p) for p in parts) > MAX_TEXT_CHARS:
                break
    return "\n".join(parts)[:MAX_TEXT_CHARS]


def _extract_docx(path):
    import docx

    document = docx.Document(path)
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)[:MAX_TEXT_CHARS]


def _extract_image_ocr(path):
    import pytesseract
    from PIL import Image

    langs = current_app.config.get("TESSERACT_LANGS", "eng")
    with Image.open(path) as img:
        text = pytesseract.image_to_string(img, lang=langs)
    return (text or "")[:MAX_TEXT_CHARS]


def index_file(file_id, app=None):
    """Index a single file: text extraction + AI caption for images."""
    app = app or current_app._get_current_object()
    with app.app_context():
        stored = db.session.get(StoredFile, file_id)
        if not stored:
            return
        index = stored.index or FileIndex(file_id=stored.id)
        if index.id is None:
            db.session.add(index)

        try:
            # OCR is best-effort for images: when tesseract is unavailable the
            # AI caption below still makes the image searchable.
            ocr_error = None
            try:
                text = extract_text(stored)
            except Exception as exc:  # noqa: BLE001
                if not stored.is_image:
                    raise
                ocr_error = exc
                text = ""
                app.logger.warning("OCR failed for image %s: %s", file_id, exc)

            caption = None
            ai_cfg = ai_service.config_for(stored.owner)
            if stored.is_image and ai_cfg["enabled"] and ai_cfg["base_url"]:
                caption = ai_service.caption_image(
                    file_service.file_path(stored), config=ai_cfg)

            if stored.is_image and ocr_error and not caption:
                raise ocr_error  # neither OCR nor AI caption produced anything

            index.extracted_text = text or None
            index.caption = caption or None
            # Content statistics (words/lines/chars) for the user to inspect
            stats_source = text or caption or ""
            index.char_count = len(stats_source) if stats_source else None
            index.word_count = len(stats_source.split()) if stats_source else None
            index.line_count = (stats_source.count("\n") + 1) if stats_source else None
            index.status = "ok"
            index.error = None
        except Exception as exc:  # noqa: BLE001 - record any extraction failure
            app.logger.exception("Indexing failed for file %s", file_id)
            index.status = "error"
            index.error = str(exc)[:1000]

        from ..models import utcnow
        index.indexed_at = utcnow()
        db.session.commit()


def index_file_async(file_id, app):
    """Run indexing in a background thread so uploads stay fast."""
    thread = threading.Thread(target=index_file, args=(file_id, app), daemon=True)
    thread.start()
    return thread
