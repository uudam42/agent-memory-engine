"""Shared pytest fixtures — in-memory SQLite database for all tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker, Session

from memory_engine.models.orm import Base
from memory_engine.models.domain import ProjectCreate, MemoryKind, MemoryNodeCreate
from memory_engine.services.memory_service import MemoryService
from memory_engine.services.project_service import ProjectService
from memory_engine.skills.recall import RecallService
from memory_engine.skills.inspect import InspectService
from memory_engine.skills.router import SkillRouter


TEST_DB_URL = "sqlite:///:memory:"


@pytest.fixture(scope="function")
def engine():
    """Fresh in-memory SQLite engine per test.

    StaticPool ensures all sessions (including those opened by the ASGI
    test client in worker threads) share the same single connection and
    therefore the same in-memory database.
    """
    _engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=_engine)
    yield _engine
    Base.metadata.drop_all(bind=_engine)
    _engine.dispose()


@pytest.fixture(scope="function")
def session(engine) -> Session:
    """DB session bound to test engine."""
    _Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = _Session()
    yield s
    s.close()


@pytest.fixture()
def project_service(session: Session) -> ProjectService:
    return ProjectService(session)


@pytest.fixture()
def memory_service(session: Session) -> MemoryService:
    return MemoryService(session)


@pytest.fixture()
def recall_service(session: Session) -> RecallService:
    return RecallService(session)


@pytest.fixture()
def inspect_service(session: Session) -> InspectService:
    return InspectService(session)


@pytest.fixture()
def skill_router() -> SkillRouter:
    return SkillRouter()


@pytest.fixture()
def demo_project(project_service: ProjectService):
    """A simple demo project used as a fixture across tests."""
    return project_service.create(
        ProjectCreate(name="demo-project", description="Test fixture project")
    )


@pytest.fixture()
def scheduler_demo(session: Session):
    """Full scheduler system demo — returns dict of named objects."""
    from tests.fixtures.scheduler_demo import create_scheduler_demo
    return create_scheduler_demo(session)


@pytest.fixture()
def api_client(engine):
    """FastAPI test client wired to the test DB engine (own session per request)."""
    from memory_engine.main import create_app
    from memory_engine.api.deps import db_session

    _Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def _override():
        s = _Session()
        try:
            yield s
        finally:
            s.close()

    _app = create_app()
    _app.dependency_overrides[db_session] = _override
    with TestClient(_app, raise_server_exceptions=True) as client:
        yield client
