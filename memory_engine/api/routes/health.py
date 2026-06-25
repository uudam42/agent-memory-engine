from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    from memory_engine import __version__
    return HealthResponse(status="ok", version=__version__)
