"""memory_engine.runtime.cache — compatibility re-export namespace.

Implementation lives in memory_engine.knowledge.cache.
"""
from memory_engine.knowledge.cache import SimpleCache, get_global_cache, normalize_query

__all__ = ["SimpleCache", "get_global_cache", "normalize_query"]
