"""Keep interactive execution state and replay events private to their initiator."""
from alembic import op

revision = '164'
down_revision = '163'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE workflow_run_state NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE workflow_run_events NO FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE workflow_run_events ADD COLUMN IF NOT EXISTS creator_user_id uuid REFERENCES users(user_id)')
    op.execute('''UPDATE workflow_run_events e SET creator_user_id=s.creator_user_id
                  FROM workflow_run_state s WHERE e.wf_id=s.wf_id AND e.tenant_id=s.tenant_id''')
    op.execute('ALTER TABLE workflow_run_events ALTER COLUMN creator_user_id SET NOT NULL')
    op.execute('ALTER TABLE workflow_run_events DROP CONSTRAINT IF EXISTS workflow_run_events_wf_id_fkey')
    op.execute('ALTER TABLE workflow_run_events DROP CONSTRAINT IF EXISTS fk_workflow_run_event_owner')
    op.execute('ALTER TABLE workflow_run_events DROP CONSTRAINT workflow_run_events_pkey')
    op.execute('ALTER TABLE workflow_run_state DROP CONSTRAINT workflow_run_state_pkey')
    op.execute('ALTER TABLE workflow_run_state ADD PRIMARY KEY (wf_id, tenant_id, creator_user_id)')
    op.execute('ALTER TABLE workflow_run_events ADD PRIMARY KEY (wf_id, tenant_id, creator_user_id, seq)')
    op.execute('''ALTER TABLE workflow_run_events ADD CONSTRAINT fk_workflow_run_event_owner
                  FOREIGN KEY (wf_id,tenant_id,creator_user_id)
                  REFERENCES workflow_run_state(wf_id,tenant_id,creator_user_id) ON DELETE CASCADE''')
    op.execute('ALTER TABLE workflow_run_events FORCE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE workflow_run_state FORCE ROW LEVEL SECURITY')


def downgrade():
    raise RuntimeError('Multiple users may have execution state; collapsing it would lose data.')
