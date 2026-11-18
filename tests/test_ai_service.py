"""S6: secret-encryption hardening — HKDF dual-read, no silent plaintext."""
import base64
import hashlib
import logging

import pytest
from cryptography.fernet import Fernet
from flask import current_app

from app.services import ai_service
from app.services.ai_service import decrypt_api_key, encrypt_api_key


def legacy_encrypt(plain):
    """SHA-256(SECRET_KEY) Fernet — the pre-HKDF scheme (test helper)."""
    secret = (current_app.config.get("SECRET_KEY") or "").encode()
    digest = hashlib.sha256(secret).digest()
    return "enc:" + Fernet(base64.urlsafe_b64encode(digest)).encrypt(
        plain.encode()).decode()


def test_legacy_secrets_still_open_new_saves_use_hkdf(app):
    with app.app_context():
        legacy = legacy_encrypt("sk-test")  # SHA-256(SECRET_KEY), test helper
        assert decrypt_api_key(legacy) == "sk-test"          # dual-read
        fresh = encrypt_api_key("sk-test")
        assert fresh != legacy and decrypt_api_key(fresh) == "sk-test"
        assert ai_service.is_hkdf_ciphertext(fresh)


def test_no_silent_plaintext_fallback(app, monkeypatch):
    with app.app_context():
        # Both Fernet factories unusable -> encrypt must raise, never echo plaintext.
        monkeypatch.setattr(ai_service, "_fernet", lambda: None)
        monkeypatch.setattr(ai_service, "_fernet_for_salt", lambda salt: None)
        with pytest.raises(RuntimeError):
            encrypt_api_key("sk-test")


def test_plaintext_row_migrated_on_read(app, caplog):
    with app.app_context():
        with caplog.at_level(logging.WARNING):
            assert decrypt_api_key("sk-plain-row") == "sk-plain-row"
        assert "plaintext" in caplog.text.lower()
