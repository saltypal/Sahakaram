"""Each test owns a fresh SQLite file. No database service or environment required."""

import pytest

from sahakaram.storage.database import create_database, default_database_url, migrate


@pytest.fixture
def database(tmp_path):
    engine = create_database(default_database_url(tmp_path))
    migrate(engine)
    try:
        yield engine
    finally:
        engine.dispose()
