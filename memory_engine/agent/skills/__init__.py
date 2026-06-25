"""memory_engine.agent.skills — compatibility re-export namespace.

All implementations reside in memory_engine.skills.
This package exposes them under the Stage 8 canonical path.

Usage (new path):
    from memory_engine.agent.skills.recall import RecallService
    from memory_engine.agent.skills.reflect import ReflectionSkill

Usage (original path, still supported):
    from memory_engine.skills.recall import RecallService
"""

from memory_engine.skills.recall import RecallService
from memory_engine.skills.inspect import InspectService
from memory_engine.skills.reflection import ReflectionSkill
from memory_engine.skills.query_analyzer import QueryAnalyzerProtocol, DeterministicQueryAnalyzer, QueryAnalysis
from memory_engine.skills.router import SkillRouter
from memory_engine.skills.ranker import DeterministicRanker
from memory_engine.skills.composer import ContextComposer

__all__ = [
    "RecallService",
    "InspectService",
    "ReflectionSkill",
    "QueryAnalyzerProtocol",
    "QueryAnalysis",
    "DeterministicQueryAnalyzer",
    "SkillRouter",
    "DeterministicRanker",
    "ContextComposer",
]
