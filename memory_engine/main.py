"""FastAPI application factory."""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI

from memory_engine import __version__
from memory_engine.api.routes import health, memories, projects
from memory_engine.api.routes import skills, candidates, reflection
from memory_engine.api.routes import knowledge, unified
from memory_engine.db.init_db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Initialising database …")
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Memory Engine",
        description=(
            "Persistent memory system for coding agents. "
            "Phase 3: candidate promotion, deduplication, conflict detection, "
            "stale-memory lifecycle."
        ),
        version=__version__,
        lifespan=_lifespan,
    )

    # Stage 1 CRUD
    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(memories.router)

    # Stage 2 skill endpoints
    app.include_router(skills.router)

    # Phase 3 candidate / lifecycle endpoints
    app.include_router(candidates.router)

    # Phase 5 post-task reflection and automatic memory writing
    app.include_router(reflection.router)

    # Phase 6 knowledge base and unified retrieval
    app.include_router(knowledge.router)
    app.include_router(unified.router)

    return app


app = create_app()
