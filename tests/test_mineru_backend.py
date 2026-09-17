import pytest

from src.utils.mineru_backend import (
    BACKEND_FALLBACKS,
    SUPPORTED_MINERU_BACKENDS,
    normalize_backend,
    resolve_backend,
    resolve_backend_from_env,
    resolve_tier,
)


@pytest.fixture(autouse=True)
def isolate_tier(monkeypatch):
    monkeypatch.delenv("MINERU_DEFAULT_TIER", raising=False)


def test_normalize_backend_accepts_supported_values():
    for value in SUPPORTED_MINERU_BACKENDS:
        assert normalize_backend(value) == value
        # Ensure case-insensitive handling
        assert normalize_backend(value.upper()) == value
    assert normalize_backend("  vlm-http-client  ") == "vlm-http-client"


def test_normalize_backend_none_or_empty():
    assert normalize_backend(None) is None
    assert normalize_backend("") is None
    assert normalize_backend("   ") is None


def test_normalize_backend_rejects_invalid():
    with pytest.raises(ValueError) as excinfo:
        normalize_backend("not-real-backend")
    assert "Unsupported MinerU backend" in str(excinfo.value)


def test_resolve_backend_keeps_hybrid_values():
    assert BACKEND_FALLBACKS == {}
    assert resolve_backend("hybrid-http-client") == "hybrid-http-client"
    assert resolve_backend("hybrid-auto-engine") == "hybrid-auto-engine"


def test_resolve_backend_passthrough():
    assert resolve_backend("vlm-http-client") == "vlm-http-client"
    assert resolve_backend(None) is None


def test_resolve_backend_from_env(monkeypatch):
    monkeypatch.setenv("MINERU_DEFAULT_BACKEND", "hybrid-http-client")
    assert resolve_backend_from_env() == "hybrid-http-client"

    monkeypatch.setenv("MINERU_DEFAULT_BACKEND", "vlm-transformers")
    assert resolve_backend_from_env() == "vlm-transformers"

    monkeypatch.delenv("MINERU_DEFAULT_BACKEND", raising=False)
    assert resolve_backend_from_env() is None

    monkeypatch.setenv("MINERU_DEFAULT_BACKEND", "bogus-backend")
    with pytest.raises(ValueError):
        resolve_backend_from_env()


def test_tier_is_snapshotted_and_old_tasks_keep_quality(monkeypatch):
    monkeypatch.setenv("MINERU_DEFAULT_TIER", "standard")
    monkeypatch.setenv("MINERU_DEFAULT_BACKEND", "vlm-http-client")
    assert resolve_backend_from_env() == "standard"
    assert resolve_tier("vlm-http-client") == "advanced"
    assert resolve_tier("pipeline") == "basic"
    assert resolve_tier("hybrid-http-client") == "standard"
    assert resolve_tier("vlm-http-client", "basic") == "basic"
    monkeypatch.setenv("MINERU_DEFAULT_TIER", "bad-tier")
    with pytest.raises(ValueError, match="Unsupported MinerU tier"):
        resolve_backend_from_env()
