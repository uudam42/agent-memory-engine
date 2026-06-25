"""Application settings — all values are env-overridable."""

from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
