"""Simple in-process TTL cache for sub-second context retrieval.

Cache keys include:
  project_id + normalized_query + sorted_files + sorted_symbols + token_budget

Invalidation triggers:
  - Document ingested / updated
  - Chunk re-indexed
  - MemoryNode promoted, stale-marked, or superseded
  - Project revision changes

The cache is intentionally simple (dict + monotonic timestamps).
No Redis, no cross-process sharing.  The goal is sub-second retrieval
for the local demo, not distributed caching.

Sensitivity:
  Sensitive raw content is never cached — only the assembled UnifiedContextPack
  (which has already been redacted at ingest time).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any


DEFAULT_TTL_SECONDS = 300   # 5 minutes


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class SimpleCache:
    """Thread-unsafe in-process TTL cache.

    In production, replace with a thread-safe or process-safe variant.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, _CacheEntry] = {}
        self._project_keys: dict[str, set[str]] = {}  # project_id → set of cache keys

    # ------------------------------------------------------------------
    # Cache key
    # ------------------------------------------------------------------

    @staticmethod
    def make_key(
        project_id: str,
        normalized_query: str,
        current_files: list[str],
        current_symbols: list[str],
        token_budget: int,
        *,
        current_branch: str | None = None,
        head_commit: str | None = None,
        working_tree_dirty: bool = False,
    ) -> str:
        payload = json.dumps({
            "pid": project_id,
            "q": normalized_query,
            "files": sorted(current_files),
            "symbols": sorted(current_symbols),
            "budget": token_budget,
            # Phase 9: branch context — different branches yield different cache entries.
            # working_tree_dirty causes a unique key when the tree is modified,
            # preventing stale results from being served after uncommitted edits.
            "branch": current_branch,
            "commit": head_commit,
            "dirty": working_tree_dirty,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    # ------------------------------------------------------------------
    # Read / Write
    # ------------------------------------------------------------------

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            del self._store[key]
            return None
        return entry.value

    def set(self, key: str, value: Any, project_id: str) -> None:
        self._store[key] = _CacheEntry(
            value=value,
            expires_at=time.monotonic() + self._ttl,
        )
        self._project_keys.setdefault(project_id, set()).add(key)

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def invalidate_project(self, project_id: str) -> int:
        """Invalidate all cache entries for a project.  Returns count removed."""
        keys = self._project_keys.pop(project_id, set())
        count = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                count += 1
        return count

    def invalidate_key(self, key: str) -> bool:
        removed = key in self._store
        self._store.pop(key, None)
        return removed

    def clear(self) -> None:
        self._store.clear()
        self._project_keys.clear()

    def size(self) -> int:
        return len(self._store)

    def stats(self) -> dict[str, int]:
        return {"size": self.size(), "projects": len(self._project_keys)}


# ---------------------------------------------------------------------------
# Module-level singleton for shared use across services in one process
# ---------------------------------------------------------------------------

_global_cache: SimpleCache = SimpleCache()


def get_global_cache() -> SimpleCache:
    return _global_cache


def normalize_query(query: str) -> str:
    """Lowercase, strip, collapse whitespace for cache-key normalization."""
    import re
    return re.sub(r"\s+", " ", query.strip().lower())
