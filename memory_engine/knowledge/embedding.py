"""Embedding provider abstraction — Phase 13.

Defines a clean Protocol plus three implementations:

  NoEmbeddingProvider           — safe default; no embeddings, semantic stays 0.0
  SentenceTransformersProvider  — optional local provider via sentence-transformers
  OllamaEmbeddingProvider       — optional local Ollama embedding provider

build_provider(config) constructs the configured provider and falls back to
NoEmbeddingProvider whenever the requested backend is disabled or unavailable.

All providers are local. No cloud embedding APIs are used.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str
    dimension: int

    def is_available(self) -> bool: ...
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, query: str) -> list[float]: ...


class NoEmbeddingProvider:
    """Safe default — no embeddings, semantic_similarity stays 0.0."""

    provider_name = "none"
    model_name = "none"
    dimension = 0

    def is_available(self) -> bool:
        return False

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]

    def embed_query(self, query: str) -> list[float]:
        return []


class SentenceTransformersProvider:
    """Optional local provider via sentence-transformers."""

    provider_name = "sentence_transformers"

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self.model_name = model_name
        self._model = None
        self.dimension = 0

    def is_available(self) -> bool:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            if self._model is None:
                self._model = SentenceTransformer(self.model_name)
                # sentence-transformers ≥3.x renamed the method; try new API first
                try:
                    self.dimension = int(self._model.get_embedding_dimension())
                except AttributeError:
                    self.dimension = int(self._model.get_sentence_embedding_dimension())
            return True
        except Exception:
            return False

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self.is_available():
            return [[] for _ in texts]
        vecs = self._model.encode(texts, normalize_embeddings=True)  # type: ignore[union-attr]
        return [list(map(float, v)) for v in vecs]

    def embed_query(self, query: str) -> list[float]:
        result = self.embed_texts([query])
        return result[0] if result else []


class OllamaEmbeddingProvider:
    """Optional local Ollama embedding provider."""

    provider_name = "ollama"

    def __init__(
        self,
        model_name: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
        timeout: float = 10.0,
    ) -> None:
        self.model_name = model_name
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.dimension = 0

    def is_available(self) -> bool:
        try:
            import httpx  # type: ignore

            r = httpx.get(f"{self._base_url}/api/tags", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        try:
            import httpx  # type: ignore
        except Exception:
            return [[] for _ in texts]

        results: list[list[float]] = []
        for t in texts:
            try:
                r = httpx.post(
                    f"{self._base_url}/api/embeddings",
                    json={"model": self.model_name, "prompt": t},
                    timeout=self._timeout,
                )
                data = r.json()
                vec = [float(x) for x in data.get("embedding", [])]
                if vec and self.dimension == 0:
                    self.dimension = len(vec)
                results.append(vec)
            except Exception:
                results.append([])
        return results

    def embed_query(self, query: str) -> list[float]:
        result = self.embed_texts([query])
        return result[0] if result else []


def build_provider(config) -> EmbeddingProvider:  # type: ignore[no-untyped-def]
    """Build the configured provider, falling back to NoEmbeddingProvider.

    config is a SemanticRetrievalSettings-like object exposing:
      enabled, provider, model, ollama_base_url, ollama_timeout
    """
    if not getattr(config, "enabled", False):
        return NoEmbeddingProvider()

    name = getattr(config, "provider", "none")
    if name == "sentence_transformers":
        p_st = SentenceTransformersProvider(model_name=config.model)
        if p_st.is_available():
            return p_st
    elif name == "ollama":
        p_ol = OllamaEmbeddingProvider(
            model_name=config.model,
            base_url=config.ollama_base_url,
            timeout=config.ollama_timeout,
        )
        if p_ol.is_available():
            return p_ol
    return NoEmbeddingProvider()
