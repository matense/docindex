"""S6 fix round: key rotation wired at the settings load/save flow.

`decrypt_api_key` stays pure (no DB writes); instead the settings page load
(and the edit-save path that keeps the old key) re-encrypts legacy/plaintext
rows with HKDF and persists them, surfacing a banner flag for the template.
"""
import base64
import hashlib

from flask import template_rendered

from app.extensions import db
from app.models import AIConnection
from app.services import ai_service


def _legacy_encrypt(secret, plain):
    """Pre-HKDF scheme: SHA-256(SECRET_KEY) Fernet (independent test helper)."""
    digest = hashlib.sha256(secret.encode()).digest()
    return "enc:" + __import__(
        "cryptography.fernet", fromlist=["Fernet"]).Fernet(
            base64.urlsafe_b64encode(digest)).encrypt(plain.encode()).decode()


def _add_stored(app, user_id, stored, name="Conn"):
    with app.app_context():
        conn = AIConnection(user_id=user_id, name=name,
                            base_url="https://api.openai.com/v1",
                            api_key=stored, model="m")
        db.session.add(conn)
        db.session.commit()
        return conn.id


def _stored(app, cid):
    with app.app_context():
        return db.session.get(AIConnection, cid).api_key


def _get_settings_page(app, client):
    """GET /settings/ai, capturing the template context (banner flag)."""
    captured = {}

    def record(sender, template, context, **kw):
        captured.update(context)

    template_rendered.connect(record, app)
    try:
        resp = client.get("/settings/ai")
    finally:
        template_rendered.disconnect(record, app)
    return resp, captured


def test_legacy_row_migrates_on_settings_load(app, auth_client, user):
    with app.app_context():
        secret = app.config.get("SECRET_KEY")
    cid = _add_stored(app, user, _legacy_encrypt(secret, "sk-test"), name="Leg")

    resp, ctx = _get_settings_page(app, auth_client)

    assert resp.status_code == 200
    migrated = _stored(app, cid)
    assert ai_service.is_hkdf_ciphertext(migrated)
    with app.app_context():
        assert ai_service.decrypt_api_key(migrated) == "sk-test"
    assert ctx["keys_rotated"] is True


def test_plaintext_row_migrates_on_settings_load(app, auth_client, user):
    cid = _add_stored(app, user, "sk-plain-row", name="Plain")

    resp, ctx = _get_settings_page(app, auth_client)

    assert resp.status_code == 200
    migrated = _stored(app, cid)
    assert ai_service.is_hkdf_ciphertext(migrated)
    with app.app_context():
        assert ai_service.decrypt_api_key(migrated) == "sk-plain-row"
    assert ctx["keys_rotated"] is True


def test_banner_flag_false_for_v2_and_empty(app, auth_client, user):
    with app.app_context():
        v2 = ai_service.encrypt_api_key("sk-x")
    cid_v2 = _add_stored(app, user, v2, name="V2")
    cid_empty = _add_stored(app, user, "", name="Empty")

    resp, ctx = _get_settings_page(app, auth_client)

    assert resp.status_code == 200
    assert ctx["keys_rotated"] is False
    assert _stored(app, cid_v2) == v2  # untouched
    assert _stored(app, cid_empty) == ""


def test_edit_save_migrates_legacy_key(app, auth_client, user):
    with app.app_context():
        secret = app.config.get("SECRET_KEY")
    cid = _add_stored(app, user, _legacy_encrypt(secret, "sk-test"), name="Old")

    resp = auth_client.post(f"/settings/ai/{cid}/edit", data={
        "name": "Old", "base_url": "https://api.openai.com/v1", "api_key": "",
        "model": "m",
    }, follow_redirects=True)

    assert resp.status_code == 200
    migrated = _stored(app, cid)
    assert ai_service.is_hkdf_ciphertext(migrated)
    with app.app_context():
        assert ai_service.decrypt_api_key(migrated) == "sk-test"
