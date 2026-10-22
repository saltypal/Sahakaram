"""Local SQLite persistence with explicit read/write transaction boundaries."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import URL, make_url


def default_database_url(state_dir: Path) -> str:
    return URL.create(
        "sqlite+pysqlite", database=str(state_dir.resolve() / "sahakaram.sqlite3")
    ).render_as_string()


def create_database(url: str) -> Engine:
    parsed = make_url(url)
    if (
        parsed.get_backend_name() != "sqlite"
        or not parsed.database
        or parsed.database == ":memory:"
    ):
        raise ValueError(
            "V1 requires a local SQLite file; existing PostgreSQL data is not modified"
        )
    path = Path(parsed.database).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(path)),
        pool_size=5,
        max_overflow=0,
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection: Any, connection_record: Any) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin(connection: Connection) -> None:
        # Reserve the single writer BEFORE reading capacity or task versions.
        statement = (
            "BEGIN IMMEDIATE" if connection.get_execution_options().get("write") else "BEGIN"
        )
        connection.exec_driver_sql(statement)

    return engine


@contextmanager
def write_transaction(engine: Engine) -> Iterator[Connection]:
    with engine.connect().execution_options(write=True) as connection, connection.begin():
        yield connection


def migrate(engine: Engine) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations_sqlite"))
    with write_transaction(engine) as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
