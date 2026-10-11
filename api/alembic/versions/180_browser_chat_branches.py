"""Allow explicit browser branches to share their source Project.

Ordinary new browser conversations still allocate independent Projects.
Downgrading requires the old uniqueness invariant to hold; never delete
branch history automatically to recreate that constraint.
"""
from alembic import op

revision = '180'
down_revision = '179'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index('uq_browser_project_chat', table_name='chats')


def downgrade():
    op.execute("CREATE UNIQUE INDEX uq_browser_project_chat ON chats (project_id) WHERE surface = 'browser'")
