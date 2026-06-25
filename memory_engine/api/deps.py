"""FastAPI dependency injectors."""

from collections.abc import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from memory_engine.db.session import get_session
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.project_service import ProjectService
from memory_engine.services.tree_service import TreeService
from memory_engine.skills.recall import RecallService
from memory_engine.skills.inspect import InspectService
from memory_engine.skills.router import SkillRouter


def db_session() -> Generator[Session, None, None]:
    yield from get_session()


SessionDep = Depends(db_session)


def project_service(session: Session = SessionDep) -> ProjectService:
    return ProjectService(session)


def memory_service(session: Session = SessionDep) -> MemoryService:
    return MemoryService(session)


def tree_service(session: Session = SessionDep) -> TreeService:
    return TreeService(session)


def recall_service(session: Session = SessionDep) -> RecallService:
    return RecallService(session)


def inspect_service(session: Session = SessionDep) -> InspectService:
    return InspectService(session)


def skill_router() -> SkillRouter:
    """SkillRouter is stateless — no DB session needed."""
    return SkillRouter()
