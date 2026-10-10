"""Record the single dispatch attempt before sending a steering input."""
from alembic import op

revision = '178'
down_revision = '177'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE chat_user_inputs ADD COLUMN dispatch_started_at timestamptz')


def downgrade():
    op.execute('ALTER TABLE chat_user_inputs DROP COLUMN dispatch_started_at')
