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


# --- Task X6: settings/auth honesty pass (TDD red first) ---


def _x6_admin_client(app, client, username, password):
    with app.app_context():
        from app.models import User

        u = User(username=username, email=username + "@example.com",
                 is_admin=True)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
    client.post("/login",
                data={"username": username, "password": password})
    return client


def test_nonadmin_settings_logs_shows_branded_forbidden(auth_client):
    resp = auth_client.get("/settings/logs")
    assert resp.status_code == 403
    html = resp.data.decode()
    assert "do not have permission" in html
    assert "Back to profile" in html


def test_nonadmin_settings_modules_shows_branded_forbidden(auth_client):
    resp = auth_client.get("/settings/modules")
    assert resp.status_code == 403
    assert b"do not have permission" in resp.data


def test_unknown_page_shows_branded_not_found(auth_client):
    resp = auth_client.get("/no-such-page-anywhere-xyz")
    assert resp.status_code == 404
    html = resp.data.decode()
    assert "does not exist or was moved" in html
    assert "Back to drive" in html


def _add_conn(app, user, name="Conn", active=True):
    from app.models import AIConnection

    with app.app_context():
        conn = AIConnection(
            user_id=user, name=name,
            base_url="https://api.openai.com/v1",
            api_key=ai_service.encrypt_api_key("sk-test"),
            model="m", is_active=active)
        db.session.add(conn)
        db.session.commit()
        return conn.id


def test_ai_activate_missing_flashes_not_found(auth_client):
    resp = auth_client.post("/settings/ai/999999/activate",
                            follow_redirects=True)
    assert resp.status_code == 200
    assert b"Connection not found" in resp.data


def test_ai_delete_missing_flashes_not_found(auth_client):
    resp = auth_client.post("/settings/ai/999999/delete",
                            follow_redirects=True)
    assert resp.status_code == 200
    assert b"Connection not found" in resp.data


def test_ai_edit_bad_id_flashes_not_found(auth_client):
    resp = auth_client.get("/settings/ai?edit=999999")
    assert resp.status_code == 200
    assert b"Connection not found" in resp.data


def test_profile_email_rejects_bad_format(app, auth_client, user):
    from app.models import User as _User

    resp = auth_client.post("/settings/profile/email",
                            data={"email": "not-an-email"},
                            follow_redirects=True)
    assert resp.status_code == 200
    assert b"valid email address" in resp.data
    with app.app_context():
        assert db.session.get(_User, user).email == "alice@example.com"


def test_ai_add_clamp_flashes_stored_values(auth_client):
    resp = auth_client.post("/settings/ai/add", data={
        "name": "Big", "base_url": "https://api.openai.com/v1",
        "model": "m", "max_steps": "1000",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"saved as 128 of requested 1000" in resp.data


def test_ai_edit_clamp_flashes_stored_values(app, auth_client, user):
    cid = _add_conn(app, user)
    resp = auth_client.post(f"/settings/ai/{cid}/edit", data={
        "name": "Conn", "base_url": "https://api.openai.com/v1",
        "model": "m", "max_steps": "1000",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"saved as 128 of requested 1000" in resp.data


def _trash_drive_with_file(app, auth_client, user,
                           drive_name="ZebraArchive", fname="keep.txt"):
    import io as _io

    from app.models import Drive, StoredFile
    from app.models import User as _User
    from app.services import drive_service as _ds

    auth_client.post("/upload", data={
        "folder_id": "",
        "files": (_io.BytesIO(b"x"), fname),
    }, content_type="multipart/form-data", follow_redirects=True)
    with app.app_context():
        u = db.session.get(_User, user)
        stored = StoredFile.query.filter_by(name=fname).one()
        drive = db.session.get(Drive, stored.drive_id)
        drive.name = drive_name
        db.session.commit()
        _ds.trash_drive(u, drive.id)
        return drive.id, stored.id


def test_trash_search_includes_drives(app, auth_client, user):
    _trash_drive_with_file(app, auth_client, user)
    hit = auth_client.get("/settings/trash?q=zebra")
    assert b"ZebraArchive" in hit.data
    miss = auth_client.get("/settings/trash?q=no-such-thing-xyz")
    assert b"ZebraArchive" not in miss.data


def test_logs_actions_disabled_when_empty(app, client):
    admin = _x6_admin_client(app, client, "boss", "adminpass123")
    with app.app_context():
        from app.models import ErrorLog

        assert ErrorLog.query.count() == 0
    resp = admin.get("/settings/logs")
    assert resp.status_code == 200
    assert b"Nothing to clear yet" in resp.data
    assert b"Nothing to export yet" in resp.data


def test_logs_clear_noop_reports_nothing_to_clear(app, client):
    admin = _x6_admin_client(app, client, "boss2", "adminpass123")
    resp = admin.post("/settings/logs/clear", follow_redirects=True)
    assert resp.status_code == 200
    assert b"nothing to clear" in resp.data


def test_key_rotation_banner_rendered(app, auth_client, user):
    with app.app_context():
        secret = app.config.get("SECRET_KEY")
    _add_stored(app, user, _legacy_encrypt(secret, "sk-test"), name="Leg")
    resp = auth_client.get("/settings/ai")
    assert resp.status_code == 200
    assert b"re-encrypted" in resp.data


def test_restore_choice_has_create_drive_link(app, auth_client, user):
    _trash_drive_with_file(app, auth_client, user)
    with app.app_context():
        from app.models import StoredFile

        fid = StoredFile.query.filter_by(name="keep.txt").one().id
    resp = auth_client.post(f"/file/{fid}/restore")
    assert resp.status_code == 200
    assert b"create one from your drives" in resp.data


def test_delete_connection_confirm_names_active_impact(app, auth_client,
                                                       user):
    _add_conn(app, user, active=True)
    resp = auth_client.get("/settings/ai")
    assert resp.status_code == 200
    assert b"until you activate another one" in resp.data


def test_auto_purge_toggle_has_confirm(auth_client):
    resp = auth_client.get("/settings/trash")
    assert resp.status_code == 200
    assert b"Turn on automatic purge?" in resp.data


def test_registration_toggle_has_confirm(app, client):
    admin = _x6_admin_client(app, client, "boss3", "adminpass123")
    resp = admin.get("/settings/profile")
    assert resp.status_code == 200
    assert b"registration setting?" in resp.data


def test_module_toggle_has_confirm(app, client):
    admin = _x6_admin_client(app, client, "boss4", "adminpass123")
    resp = admin.get("/settings/modules")
    assert resp.status_code == 200
    assert b"module &#34;hello&#34;?" in resp.data
