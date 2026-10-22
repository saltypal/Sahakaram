"""Migrations use the supplied connection, avoiding credentials in configuration files."""

from alembic import context

from sahakaram.storage.schema import metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use sahakaram db-upgrade with an configured local SQLite database")
context.configure(connection=connection, target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
