"""Application settings — all values are env-overridable."""

import os

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SemanticRetrievalSettings(BaseModel):
    """Phase 13 — local persistent semantic retrieval configuration.

    Default install stays lightweight: enabled=False, provider=none.
    No model downloads, no extra required dependencies until explicitly enabled.
    """

    enabled: bool = False
    provider: str = "none"          # none | sentence_transformers | ollama
    model: str = "BAAI/bge-small-en-v1.5"
    backend: str = "sqlite_vec"     # none | sqlite_vec | qdrant_embedded
    top_k: int = 20
    fusion_mode: str = "rrf"
    rrf_k: int = 60
    lexical_weight: float = 1.0
    semantic_weight: float = 1.0
    ollama_base_url: str = "http://localhost:11434"
    ollama_timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "SemanticRetrievalSettings":
        """Build settings, honouring the common top-level env overrides.

        Top-level vars take precedence over defaults so operators can flip
        semantic retrieval on without editing a config file:

            MEMORY_ENGINE_SEMANTIC_ENABLED
            MEMORY_ENGINE_EMBEDDING_PROVIDER
            MEMORY_ENGINE_EMBEDDING_MODEL
            MEMORY_ENGINE_VECTOR_BACKEND
            MEMORY_ENGINE_OLLAMA_BASE_URL
        """
        inst = cls()
        enabled = os.environ.get("MEMORY_ENGINE_SEMANTIC_ENABLED")
        if enabled is not None:
            inst.enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
        provider = os.environ.get("MEMORY_ENGINE_EMBEDDING_PROVIDER")
        if provider:
            inst.provider = provider.strip()
        model = os.environ.get("MEMORY_ENGINE_EMBEDDING_MODEL")
        if model:
            inst.model = model.strip()
        backend = os.environ.get("MEMORY_ENGINE_VECTOR_BACKEND")
        if backend:
            inst.backend = backend.strip()
        ollama_url = os.environ.get("MEMORY_ENGINE_OLLAMA_BASE_URL")
        if ollama_url:
            inst.ollama_base_url = ollama_url.strip()
        return inst


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ME_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./memory_engine.db"
    log_level: str = "INFO"
    max_tree_depth: int = 10        # enforced on write

    # Stage 2 — context budgets (tokens)
    default_token_budget: int = 6000
    budget_constraints: int = 800
    budget_architecture: int = 900
    budget_modules: int = 1800
    budget_decisions_incidents: int = 1700
    budget_procedures: int = 500
    budget_evidence: int = 300

    # Phase 13 — semantic retrieval (default off)
    semantic: SemanticRetrievalSettings = Field(
        default_factory=SemanticRetrievalSettings.from_env
    )


settings = Settings()
