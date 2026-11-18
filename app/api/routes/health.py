from fastapi import APIRouter

from app import __version__
from app.core.config import settings

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "env": settings.app_env}
