"""Explicit PostgreSQL-only engine and migration entry point."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url


def create_database(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("Sahakaram requires PostgreSQL; other database engines are unsupported")
    parsed = parsed.set(drivername="postgresql+psycopg")
    return create_engine(
        parsed, pool_size=5, max_overflow=0, pool_pre_ping=True, connect_args={"connect_timeout": 5}
    )


def migrate(engine: Engine) -> None:
    """Upgrade only the explicitly selected database; never downgrade or delete data."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
