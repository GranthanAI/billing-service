"""${message}"""

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None

from alembic import op


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
