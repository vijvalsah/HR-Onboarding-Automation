from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.routes import api_router
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.seed import seed
from app.db.session import SessionLocal, init_db

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "Application startup (app=%s env=%s)",
        settings.app_name,
        settings.app_env,
    )
    # Ensure tables exist and demo data is present. init_db is a no-op if tables
    # already exist; seed is a no-op if users already exist — both idempotent.
    await init_db()
    async with SessionLocal() as db:
        await seed(db)
    yield
    logger.info("Application shutdown")


def create_app() -> FastAPI:
    setup_logging()

    application = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
        version=__version__,
    )
    application.include_router(api_router, prefix=settings.api_v1_prefix)
    return application


app = create_app()
