"""Project-local config.yaml management.

Generated automatically on first bootstrap.
User edits are preserved on subsequent runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "project": {
        "name": "auto",
        "root_path": "auto",
    },
    "runtime": {
        "auto_bootstrap": True,
        "auto_recall": True,
        "auto_reflect": True,
        "auto_index_on_start": True,
        "incremental_indexing": True,
        # Phase 9: Git-aware synchronization
        "git_aware_sync": True,
        "check_git_status_on_retrieval": True,
        "auto_incremental_sync": True,
        "optional_file_watcher": False,
        "watcher_debounce_ms": 1000,
    },
    "retrieval": {
        "default_token_budget": 6000,
        "cache_enabled": True,
        "vector_backend": "auto",
        "allow_degraded_fallback": True,
        # Phase 9: branch-aware retrieval
        "branch_aware_ranking": True,
        "prefer_current_branch": True,
        "include_ancestor_branch_memory": True,
        "include_mainline_fallback": True,
        "include_historical_branch_memory_by_default": False,
    },
    "memory": {
        # Phase 9: branch-scoped memory policy
        "branch_scope_on_feature_work": "current_branch",
        "mainline_promotion_requires_confirmation": True,
        "branch_memory_retention_days": None,
    },
    "privacy": {
        "mode": "local",
        "redact_secrets": True,
        "allow_network_embedding": False,
        # Phase 9: Git identity protection
        "expose_git_remote_url": False,
        "redact_git_identity": True,
    },
    "memory_retention": {
        "enabled": True,
        "candidate_expiry_days": 30,
        "inactive_archive_days": 120,
        "stale_archive_days": 180,
        "compaction": {
            "enabled": True,
            "min_related_memories": 3,
            "min_age_days": 14,
            "require_same_parent_scope": True,
            "require_same_memory_type": True,
            "preserve_original_evidence": True,
            "auto_archive_source_memories": True,
        },
        "branch_cleanup": {
            "enabled": True,
            "archive_deleted_branch_memories": True,
            "archive_merged_branch_memories_after_days": 30,
            "preserve_promoted_mainline_knowledge": True,
        },
        "protected_memory_types": [
            "constraint",
            "security_rule",
            "architecture",
            "decision",
        ],
    },
    "knowledge": {
        "include": [
            "README.md",
            "README.rst",
            "docs/**",
            "src/**",
            "app/**",
            "lib/**",
            "tests/**",
            "*.md",
        ],
        "exclude": [
            "node_modules/**",
            ".git/**",
            ".venv/**",
            "venv/**",
            "dist/**",
            "build/**",
            "target/**",
            "coverage/**",
            ".env",
            "secrets/**",
        ],
        "max_file_size_mb": 5,
    },
}


def load_config(config_path: Path) -> dict[str, Any]:
    """Load config.yaml, falling back to defaults for missing keys."""
    if not config_path.exists():
        return dict(DEFAULT_CONFIG)

    with open(config_path, encoding="utf-8") as f:
        user_config = yaml.safe_load(f) or {}

    # Deep merge: user values win over defaults
    return _deep_merge(DEFAULT_CONFIG, user_config)


def write_default_config(config_path: Path, project_name: str = "auto") -> None:
    """Write default config.yaml only if it does not already exist.

    Preserves user modifications.
    """
    if config_path.exists():
        return  # never overwrite user edits

    cfg = dict(DEFAULT_CONFIG)
    cfg["project"] = dict(cfg["project"])
    cfg["project"]["name"] = project_name

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def write_semantic_config(
    config_path: Path,
    enabled: bool = True,
    provider: str = "sentence_transformers",
    model: str = "BAAI/bge-small-en-v1.5",
) -> None:
    """Persist semantic retrieval settings into config.yaml.

    Creates the file if absent; preserves all other existing sections.
    Env vars (MEMORY_ENGINE_SEMANTIC_ENABLED etc.) still take precedence at runtime.
    """
    existing: dict = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}

    existing.setdefault("semantic", {})
    existing["semantic"]["enabled"] = enabled
    existing["semantic"]["provider"] = provider
    existing["semantic"]["model"] = model

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(existing, f, default_flow_style=False, sort_keys=False)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base.  Override wins on conflicts."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result
