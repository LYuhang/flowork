"""Associate internal Chat Projects with a Workflow without duplicating Chat storage."""
from alembic import op

revision = "158"
down_revision = "157"
branch_labels = None
depends_on = None


def upgrade():
    # Early bootstrap migrations use current metadata on fresh databases.
    op.execute("ALTER TABLE chat_projects ADD COLUMN IF NOT EXISTS workflow_id text REFERENCES workflows(wf_id) ON DELETE RESTRICT")
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_project_workflow_owner
        ON chat_projects (tenant_id, creator_user_id, workflow_id)
        WHERE workflow_id IS NOT NULL AND deleted_at IS NULL""")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM chat_projects WHERE workflow_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Remove Workflow Chat Projects before downgrading';
        END IF;
    END $$""")
    op.execute("DROP INDEX IF EXISTS uq_project_workflow_owner")
    op.execute("ALTER TABLE chat_projects DROP COLUMN workflow_id")
