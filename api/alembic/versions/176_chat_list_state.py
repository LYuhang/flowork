"""Persist private conversation pin/archive preferences without changing runs."""
from alembic import op

revision = '176'
down_revision = '175'
branch_labels = None
depends_on = None


def upgrade():
    # Revision 001 creates the current metadata on a fresh installation.
    op.execute('ALTER TABLE chats ADD COLUMN IF NOT EXISTS pinned_at timestamptz')
    op.execute('ALTER TABLE chats ADD COLUMN IF NOT EXISTS archived_at timestamptz')


def downgrade():
    op.execute('ALTER TABLE chats DROP COLUMN archived_at')
    op.execute('ALTER TABLE chats DROP COLUMN pinned_at')
