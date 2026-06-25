"""memory_engine.runtime.bootstrap — compatibility re-export namespace.

All implementations reside in memory_engine.bootstrap.
This package exposes them under the Stage 8 canonical path.

Usage (new path):
    from memory_engine.runtime.bootstrap.bootstrap_service import ProjectBootstrapService
    from memory_engine.runtime.bootstrap.security import resolve_safe

Usage (original path, still supported):
    from memory_engine.bootstrap.bootstrap_service import ProjectBootstrapService
"""

from memory_engine.bootstrap.bootstrap_service import ProjectBootstrapService
from memory_engine.bootstrap.project_root import resolve_project_root, ProjectRootError
from memory_engine.bootstrap.local_storage import ProjectLocalStorage
from memory_engine.bootstrap.project_state import ProjectStateManager, BootstrapStatus
from memory_engine.bootstrap.config import load_config, write_default_config
from memory_engine.bootstrap.incremental_index import IncrementalIndexCoordinator, ChangeSet
from memory_engine.bootstrap.vector_fallback import detect_retrieval_mode, RetrievalModeInfo
from memory_engine.bootstrap.security import (
    resolve_safe,
    is_indexable,
    is_safe_symlink,
    is_excluded,
    PathEscapeError,
)

__all__ = [
    "ProjectBootstrapService",
    "resolve_project_root",
    "ProjectRootError",
    "ProjectLocalStorage",
    "ProjectStateManager",
    "BootstrapStatus",
    "load_config",
    "write_default_config",
    "IncrementalIndexCoordinator",
    "ChangeSet",
    "detect_retrieval_mode",
    "RetrievalModeInfo",
    "resolve_safe",
    "is_indexable",
    "is_safe_symlink",
    "is_excluded",
    "PathEscapeError",
]
