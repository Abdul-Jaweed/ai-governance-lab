"""FastAPI application factory (headless backend; no frontend)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.governance import router as governance_router
from app.config import Settings, get_settings
from app.db.repositories import PostgresRecorder


def create_app(
    *,
    settings: Settings | None = None,
    recorder: PostgresRecorder | None = None,
) -> FastAPI:
    """Build the app.

    A recorder may be injected (tests do this); otherwise one is created
    lazily from settings on first use.
    """

    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if recorder is None:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            engine = create_async_engine(settings.database_url, pool_pre_ping=True)
            app.state.engine = engine
            app.state.recorder = PostgresRecorder(async_sessionmaker(bind=engine))
        else:
            app.state.recorder = recorder
        try:
            yield
        finally:
            engine = getattr(app.state, "engine", None)
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="AI Governance Runtime Action Governor",
        version="0.1.0",
        description=(
            "Read-only governance evidence for agent tool executions. "
            "Authorization is enforced in the agent runtime, not here."
        ),
        lifespan=lifespan,
    )

    app.state.recorder = recorder
    app.state.settings = settings

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(governance_router)
    return app


app = create_app()
