"""memory_engine.agent.policies — deterministic rules governing agent behavior.

Currently the reflection gate logic lives in memory_engine.skills.reflection.
This namespace is reserved for future extraction into standalone policy modules.
"""
# Reflection gate constants (re-exported for reference)
from memory_engine.skills.reflection import (
    _MIN_CONFIDENCE_UNVERIFIED as MIN_CONFIDENCE_UNVERIFIED,
    _MIN_SUMMARY_WORDS as MIN_SUMMARY_WORDS,
    _VERIFICATION_CONFIDENCE as VERIFICATION_CONFIDENCE,
)

__all__ = [
    "MIN_CONFIDENCE_UNVERIFIED",
    "MIN_SUMMARY_WORDS",
    "VERIFICATION_CONFIDENCE",
]
