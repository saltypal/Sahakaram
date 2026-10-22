"""Initial schema using the frozen revision-0001 metadata."""

from alembic import op

from sahakaram.storage.schema_v1 import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Destructive schema downgrade is intentionally unsupported")
