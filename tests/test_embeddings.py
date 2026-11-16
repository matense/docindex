"""B2: per-connection embedding circuit breaker must isolate outages."""
import pytest

from app.services import ai_service, embedding_service


def test_backend_outage_isolated_per_connection():
    bad = ("http://user-a-bad-host:11434", "model")
    good = ("http://good-host:11434", "model")
    embedding_service._note_backend_down(*bad)
    try:
        with pytest.raises(ai_service.AIError):
            embedding_service._check_backend_up(*bad)
        # A different backend must NOT fail fast.
        embedding_service._check_backend_up(*good)
    finally:
        embedding_service._note_backend_up(*bad)
        embedding_service._note_backend_up(*good)
