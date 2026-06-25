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
    },
    "privacy": {
        "mode": "local",
        "redact_secrets": True,
        "allow_network_embedding": False,
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
    "retrieval": {
        "default_token_budget": 6000,
        "cache_enabled": True,
        "vector_backend": "auto",
        "allow_degraded_fallback": True,
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


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base.  Override wins on conflicts."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result
