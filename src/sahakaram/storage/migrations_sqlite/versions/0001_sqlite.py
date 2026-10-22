"""SQLite V1 initial schema, independent of the historical PostgreSQL revision."""

from alembic import op

from sahakaram.storage.sqlite_v1 import metadata

revision = "sqlite_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is intentionally unsupported")
