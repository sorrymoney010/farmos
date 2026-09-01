"""FARMOS API entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.logging import setup_logging, get_logger
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.audit import AuditMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.db.session import init_db, close_db
from app.api import api_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("farmos_starting", app_env=settings.APP_ENV)
    await init_db()
    yield
    await close_db()
    logger.info("farmos_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="FARMOS API",
        description="Distributed Edge Revenue Operating System",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(AuditMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        requests=settings.RATE_LIMIT_REQUESTS,
        window=settings.RATE_LIMIT_WINDOW_SECONDS,
    )
    app.include_router(api_router)

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "app_env": settings.APP_ENV,
            "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }

    @app.get("/metrics")
    async def metrics():
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST  # noqa: PLC0415
        from fastapi.responses import Response  # noqa: PLC0415
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
