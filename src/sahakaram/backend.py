"""Standalone collaboration backend. No MCP/plugin/Codex imports or runtime required."""

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from filelock import FileLock

from sahakaram.config import Settings
from sahakaram.hub import create_hub
from sahakaram.security import Tokens, local_secret
from sahakaram.services import Rooms
from sahakaram.storage.database import create_database, default_database_url, migrate


def create_backend(settings: Settings, *, session_minutes: int = 15) -> FastAPI:
    url = (
        settings.database_url.get_secret_value()
        if settings.database_url
        else default_database_url(settings.state_dir)
    )
    engine = create_database(url)
    try:
        migrate(engine)
        service = Rooms(
            engine,
            Tokens(
                local_secret(settings.state_dir / "host-signing.key"), ttl_minutes=session_minutes
            ),
        )
        application = create_hub(
            engine, service, local_secret(settings.state_dir / "host-bootstrap.key")
        )
    except BaseException:
        engine.dispose()
        raise
    original = application.router.lifespan_context

    @asynccontextmanager
    async def managed(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with original(app):
                yield
        finally:
            engine.dispose()

    application.router.lifespan_context = managed
    return application


def run_backend(settings: Settings, *, port: int = 8788, session_minutes: int = 15) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.state_dir / "room-host.lock"), timeout=0):
        application = create_backend(settings, session_minutes=session_minutes)
        uvicorn.run(application, host="127.0.0.1", port=port, log_level=settings.log_level.lower())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sahakaram standalone SQLite collaboration backend"
    )
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--session-minutes", type=int, default=15)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535 or not 1 <= arguments.session_minutes <= 1440:
        parser.error("Port must be 1-65535 and session minutes 1-1440")
    run_backend(Settings(), port=arguments.port, session_minutes=arguments.session_minutes)


if __name__ == "__main__":
    main()
