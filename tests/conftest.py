"""Real PostgreSQL tests use a fresh, uniquely named schema, never application tables."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from sahakaram.storage.database import migrate


@pytest.fixture
def database():
    url = os.environ.get("SAHAKARAM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set SAHAKARAM_TEST_DATABASE_URL to an explicitly disposable PostgreSQL DB")
    namespace = "sahakaram_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as c:
        c.exec_driver_sql(f'CREATE SCHEMA "{namespace}"')
    engine = create_engine(
        url, connect_args={"options": f"-csearch_path={namespace}"}, pool_size=8, max_overflow=0
    )
    try:
        migrate(engine)
        yield engine
    finally:
        engine.dispose()
        assert namespace.startswith("sahakaram_test_") and len(namespace) == 47
        with admin.begin() as c:
            c.exec_driver_sql(f'DROP SCHEMA "{namespace}" CASCADE')
        admin.dispose()
