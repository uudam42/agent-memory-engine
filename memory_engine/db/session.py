"""Database engine and session factory."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from memory_engine.config import settings

engine = create_engine(
    settings.database_url,
    # keep connections alive within a thread for SQLite
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
    echo=False,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_session() -> Generator[Session, None, None]:
    """Yield a DB session and close it when the caller is done."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
