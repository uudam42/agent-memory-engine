"""memory_engine.runtime.config — compatibility re-export namespace."""
from memory_engine.bootstrap.config import load_config, write_default_config, DEFAULT_CONFIG
from memory_engine.config import Settings

__all__ = ["load_config", "write_default_config", "DEFAULT_CONFIG", "Settings"]
