"""Phase 13 — embedding provider tests."""

from __future__ import annotations

import pytest

from memory_engine.config import SemanticRetrievalSettings
from memory_engine.knowledge.embedding import (
    NoEmbeddingProvider,
    OllamaEmbeddingProvider,
    SentenceTransformersProvider,
    build_provider,
)


def test_no_embedding_provider_is_inert():
    p = NoEmbeddingProvider()
    assert p.is_available() is False
    assert p.dimension == 0
    assert p.provider_name == "none"
    assert p.embed_texts(["a", "b"]) == [[], []]
    assert p.embed_query("anything") == []


def test_build_provider_disabled_returns_noop():
    cfg = SemanticRetrievalSettings(enabled=False, provider="sentence_transformers")
    assert isinstance(build_provider(cfg), NoEmbeddingProvider)


def test_build_provider_unavailable_falls_back_to_noop(monkeypatch):
    cfg = SemanticRetrievalSettings(enabled=True, provider="sentence_transformers")
    monkeypatch.setattr(
        SentenceTransformersProvider, "is_available", lambda self: False
    )
    assert isinstance(build_provider(cfg), NoEmbeddingProvider)


def test_build_provider_unknown_name_returns_noop():
    cfg = SemanticRetrievalSettings(enabled=True, provider="does_not_exist")
    assert isinstance(build_provider(cfg), NoEmbeddingProvider)


def test_sentence_transformers_with_mocked_model(monkeypatch):
    class _FakeModel:
        def get_sentence_embedding_dimension(self):
            return 4

        def encode(self, texts, normalize_embeddings=True):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    p = SentenceTransformersProvider(model_name="fake-model")
    # Inject the fake model and mark available.
    p._model = _FakeModel()
    p.dimension = 4
    monkeypatch.setattr(p, "is_available", lambda: True)

    vecs = p.embed_texts(["x", "y"])
    assert vecs == [[0.1, 0.2, 0.3, 0.4], [0.1, 0.2, 0.3, 0.4]]
    assert p.embed_query("z") == [0.1, 0.2, 0.3, 0.4]


def test_sentence_transformers_unavailable_returns_empty(monkeypatch):
    p = SentenceTransformersProvider(model_name="fake")
    monkeypatch.setattr(p, "is_available", lambda: False)
    assert p.embed_texts(["a"]) == [[]]


def test_ollama_provider_success(monkeypatch):
    import memory_engine.knowledge.embedding as emb_mod

    class _Resp:
        status_code = 200

        def json(self):
            return {"embedding": [0.5, 0.6, 0.7]}

    fake_httpx = type("httpx", (), {
        "get": staticmethod(lambda *a, **k: _Resp()),
        "post": staticmethod(lambda *a, **k: _Resp()),
    })
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    p = OllamaEmbeddingProvider(model_name="nomic-embed-text")
    assert p.is_available() is True
    out = p.embed_texts(["hello"])
    assert out == [[0.5, 0.6, 0.7]]
    assert p.dimension == 3
    assert p.embed_query("hi") == [0.5, 0.6, 0.7]


def test_ollama_provider_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    fake_httpx = type("httpx", (), {
        "get": staticmethod(_boom),
        "post": staticmethod(_boom),
    })
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    p = OllamaEmbeddingProvider()
    assert p.is_available() is False
    assert p.embed_texts(["x"]) == [[]]


def test_semantic_settings_env_override(monkeypatch):
    monkeypatch.setenv("MEMORY_ENGINE_SEMANTIC_ENABLED", "true")
    monkeypatch.setenv("MEMORY_ENGINE_EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("MEMORY_ENGINE_EMBEDDING_MODEL", "custom-model")
    cfg = SemanticRetrievalSettings.from_env()
    assert cfg.enabled is True
    assert cfg.provider == "ollama"
    assert cfg.model == "custom-model"
