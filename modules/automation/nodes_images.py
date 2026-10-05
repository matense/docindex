"""Image nodes: AI captions, metadata, OCR.

Registered from nodes.py::register_defaults. Images flow as ``files``
payloads (load_file_payload hands image files over as references); each
node accepts either a connected ``in`` payload or a picked file.
"""

from app.extensions import db
from app.models import StoredFile
from app.services import ai_service, file_service, indexing_service

from .engine import WorkflowError, as_files, p_json, p_text
from .registry import register_node_type


def _resolve_image(ctx, config, inputs):
    """The StoredFile to work on: the connected file payload, else the
    configured static file. Must be an image the user owns."""
    file_id = None
    payload = inputs.get("in")
    if payload is not None:
        refs = as_files(payload)
        if refs:
            file_id = refs[0].get("file_id")
    if file_id in (None, ""):
        file_id = config.get("file_id")
    try:
        stored = db.session.get(StoredFile, int(file_id))
    except (TypeError, ValueError):
        stored = None
    if (stored is None or stored.user_id != ctx["user"].id
            or stored.deleted_at is not None):
        raise WorkflowError("Pick an image file (or connect one).")
    if not stored.is_image:
        raise WorkflowError(f"'{stored.name}' is not an image file.")
    return stored


def _exec_caption_image(ctx, config, inputs):
    stored = _resolve_image(ctx, config, inputs)
    cfg = ai_service.config_for(ctx["user"])
    if not (cfg.get("enabled") and cfg.get("base_url")
            and cfg.get("model")):
        raise WorkflowError("AI is not configured — add a connection in "
                            "AI Settings.")
    instruction = (config.get("instruction") or "").strip() or None
    ctx["log"](f"captioning '{stored.name}'"
               + (f" → {cfg.get('vision_model') or cfg['model']}"))
    try:
        caption = ai_service.caption_image(
            file_service.file_path(stored), config=cfg, block=True,
            instruction=instruction)
    except ai_service.AIError as exc:
        raise WorkflowError(str(exc))
    if not caption:
        raise WorkflowError("The vision model returned an empty caption.")
    return p_text(caption)


def _exec_image_info(ctx, config, inputs):
    from PIL import Image
    stored = _resolve_image(ctx, config, inputs)
    with Image.open(file_service.file_path(stored)) as img:
        info = {"name": stored.name, "width": img.width,
                "height": img.height, "format": img.format,
                "mode": img.mode, "size_bytes": stored.size}
    ctx["log"](f"{stored.name}: {info['width']}×{info['height']} "
               f"{info['format']}")
    return p_json(info)


def _exec_ocr_image(ctx, config, inputs):
    stored = _resolve_image(ctx, config, inputs)
    if not indexing_service.ocr_available():
        raise WorkflowError("OCR needs the tesseract binary on the server's "
                            "PATH — it is not installed.")
    text = indexing_service.ocr_image(file_service.file_path(stored))
    if not text.strip():
        raise WorkflowError(f"No text found in '{stored.name}'.")
    ctx["log"](f"OCR '{stored.name}': {len(text)} chars")
    return p_text(text)


def register_image_nodes():
    """Image nodes — called once from register_defaults."""
    register_node_type("ai.caption_image", {
        "label": "Caption image with AI", "icon": "fa-image",
        "category": "AI",
        "description": "Describes an image with your vision model. "
                       "Connect a file or pick one below; steer the "
                       "description with a custom instruction.",
        "inputs": [{"name": "in", "kind": "files"}],
        "outputs": [{"name": "out", "kind": "text"}],
        "fields": [
            {"name": "file_id", "label": "Image (when nothing is "
                                         "connected)", "kind": "file",
             "default": ""},
            {"name": "instruction", "label": "Instruction", "kind":
             "textarea", "default": "",
             "placeholder": "Optional — how the image should be described",
             "presets": [
                 {"label": "Detailed caption",
                  "value": "Describe this image in detail: objects, people, "
                           "scenes, colors, any visible text, and the "
                           "overall context."},
                 {"label": "What text is visible?",
                  "value": "Transcribe every piece of text visible in this "
                           "image. Answer with the transcription only."},
                 {"label": "Product listing",
                  "value": "Describe this image for an online product "
                           "listing: what the item is, its condition, "
                           "colors and notable details."},
             ]},
        ]}, _exec_caption_image)

    register_node_type("transform.image_info", {
        "label": "Image info", "icon": "fa-circle-info",
        "category": "Transform",
        "description": "Reads an image's metadata: dimensions, format, "
                       "color mode and file size.",
        "example": "photo.png → {\"width\": 1024, \"height\": 768, "
                   "\"format\": \"PNG\", …}",
        "inputs": [{"name": "in", "kind": "files"}],
        "outputs": [{"name": "out", "kind": "json"}],
        "fields": [
            {"name": "file_id", "label": "Image (when nothing is "
                                         "connected)", "kind": "file",
             "default": ""},
        ]}, _exec_image_info)

    register_node_type("transform.ocr_image", {
        "label": "Extract text (OCR)", "icon": "fa-font",
        "category": "Transform",
        "description": "Extracts the text visible in an image with OCR "
                       "(needs tesseract installed on the server).",
        "example": "scan.png → the document's text, ready for an AI "
                   "prompt.",
        "inputs": [{"name": "in", "kind": "files"}],
        "outputs": [{"name": "out", "kind": "text"}],
        "fields": [
            {"name": "file_id", "label": "Image (when nothing is "
                                         "connected)", "kind": "file",
             "default": ""},
        ]}, _exec_ocr_image)
