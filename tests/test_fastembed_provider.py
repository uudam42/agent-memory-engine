"""Phase 13 — FastEmbed embedding provider tests.

All tests use monkeypatching / fake classes.
No real model is downloaded during the test run (matches the convention described
in the README: "Semantic retrieval tests use mocked providers or
pytest.importorskip... no models are downloaded during the test run.").
"""

from __future__ import annotations

import math
import sys

import pytest

from memory_engine.config import SemanticRetrievalSettings
from memory_engine.knowledge.embedding import (
    FastEmbedProvider,
    NoEmbeddingProvider,
    build_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeTextEmbedding:
    """Minimal stand-in for fastembed.TextEmbedding."""

    DIMENSION = 4

    def __init__(self, model_name: str, **kwargs) -> None:
        self.model_name = model_name

    def embed(self, texts):
        """Return one fake vector per text (NOT normalised — mirrors real FastEmbed)."""
        for _ in texts:
            # Raw un-normalised vector; provider must normalise it
            yield [2.0, 0.0, 0.0, 0.0]


def _install_fake_fastembed(monkeypatch):
    """Inject a fake `fastembed` module so no real package is needed."""
    fake_module = type(sys)("fastembed")
    fake_module.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)
    return fake_module


# ---------------------------------------------------------------------------
# Provider unit tests
# ---------------------------------------------------------------------------

def test_fastembed_provider_attributes():
    p = FastEmbedProvider(model_name="BAAI/bge-small-en-v1.5")
    assert p.provider_name == "fastembed"
    assert p.model_name == "BAAI/bge-small-en-v1.5"
    assert p.dimension == 0  # not yet initialised


def test_fastembed_with_mocked_model(monkeypatch):
    _install_fake_fastembed(monkeypatch)

    p = FastEmbedProvider(model_name="BAAI/bge-small-en-v1.5")
    assert p.is_available() is True
    # Dimension probe should have set this
    assert p.dimension == _FakeTextEmbedding.DIMENSION

    vecs = p.embed_texts(["hello", "world"])
    assert len(vecs) == 2
    for vec in vecs:
        assert len(vec) == _FakeTextEmbedding.DIMENSION


def test_fastembed_embed_texts_l2_normalised(monkeypatch):
    """Vectors returned by embed_texts must have unit L2 norm."""
    _install_fake_fastembed(monkeypatch)

    p = FastEmbedProvider(model_name="test-model")
    vecs = p.embed_texts(["normalisation test"])
    assert vecs, "expected at least one vector"
    vec = vecs[0]
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-6, f"expected unit norm, got {norm}"


def test_fastembed_embed_query_matches_embed_texts(monkeypatch):
    _install_fake_fastembed(monkeypatch)

    p = FastEmbedProvider(model_name="test-model")
    query_vec = p.embed_query("test query")
    texts_vec = p.embed_texts(["test query"])[0]
    assert query_vec == texts_vec


def test_fastembed_unavailable_returns_empty_lists(monkeypatch):
    """When the provider is unavailable, embed_texts returns empty lists."""
    p = FastEmbedProvider(model_name="no-such-model")
    # Ensure fastembed is NOT importable
    monkeypatch.setitem(sys.modules, "fastembed", None)

    assert p.is_available() is False
    assert p.embed_texts(["a", "b"]) == [[], []]
    assert p.embed_query("x") == []


def test_fastembed_is_available_false_when_import_fails(monkeypatch):
    """is_available() must not raise -- it should silently return False."""
    # Remove fastembed from modules so the import inside is_available() fails
    monkeypatch.setitem(sys.modules, "fastembed", None)

    p = FastEmbedProvider(model_name="any")
    result = p.is_available()
    assert result is False


# ---------------------------------------------------------------------------
# build_provider dispatch tests
# ---------------------------------------------------------------------------

def test_build_provider_dispatches_fastembed(monkeypatch):
    _install_fake_fastembed(monkeypatch)

    cfg = SemanticRetrievalSettings(enabled=True, provider="fastembed", model="BAAI/bge-small-en-v1.5")
    provider = build_provider(cfg)
    assert isinstance(provider, FastEmbedProvider)
    assert provider.provider_name == "fastembed"


def test_build_provider_fastembed_unavailable_falls_back_to_noop(monkeypatch):
    """If fastembed cannot be imported, build_provider must fall back to NoEmbeddingProvider."""
    monkeypatch.setitem(sys.modules, "fastembed", None)

    cfg = SemanticRetrievalSettings(enabled=True, provider="fastembed", model="BAAI/bge-small-en-v1.5")
    provider = build_provider(cfg)
    assert isinstance(provider, NoEmbeddingProvider)


def test_build_provider_disabled_ignores_fastembed():
    cfg = SemanticRetrievalSettings(enabled=False, provider="fastembed")
    assert isinstance(build_provider(cfg), NoEmbeddingProvider)


# ---------------------------------------------------------------------------
# Env-var selection test
# ---------------------------------------------------------------------------

def test_env_override_selects_fastembed(monkeypatch):
    monkeypatch.setenv("MEMORY_ENGINE_SEMANTIC_ENABLED", "true")
    monkeypatch.setenv("MEMORY_ENGINE_EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setenv("MEMORY_ENGINE_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")

    cfg = SemanticRetrievalSettings.from_env()
    assert cfg.enabled is True
    assert cfg.provider == "fastembed"
    assert cfg.model == "BAAI/bge-small-en-v1.5"


# ---------------------------------------------------------------------------
# Lazy-import guarantee: importing the module must NOT import fastembed
# ---------------------------------------------------------------------------

def test_fastembed_not_imported_at_module_load():
    """Importing memory_engine.knowledge.embedding must never import fastembed.

    This ensures the base install (uv sync, no extras) is not broken.
    """
    # fastembed should not appear in sys.modules simply from having imported
    # the embedding module at the top of this test file.
    assert "fastembed" not in sys.modules or sys.modules.get("fastembed") is None, (
        "fastembed was imported at module level -- it must only be imported lazily "
        "inside is_available() / embed_texts()."
    )
