"""Durable workflow execution history and human approval commands.

These records are history, not checkpoints. A lost runtime is never resumed
from these tables. Business payloads use the existing envelope encryption.
"""

from alembic import op

revision = "148"
down_revision = "147"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE workflow_execution_runs (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(tenant_id),
        wf_id text NOT NULL,
        source_type text NOT NULL CHECK (source_type IN ('workflow','task','deployment')),
        source_id text NOT NULL,
        initiator_user_id uuid REFERENCES users(user_id),
        revision_id text,
        generation text,
        status text NOT NULL DEFAULT 'queued' CHECK (status IN
            ('queued','running','waiting_approval','succeeded','failed','timed_out','cancelled')),
        last_seq bigint NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
        error_code text,
        cancel_requested_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        started_at timestamptz,
        finished_at timestamptz,
        private_ciphertext text NOT NULL,
        private_nonce text NOT NULL,
        private_key_id uuid NOT NULL REFERENCES content_encryption_keys(key_id),
        result_ciphertext text,
        result_nonce text,
        result_key_id uuid REFERENCES content_encryption_keys(key_id),
        UNIQUE (tenant_id, id)
    )""")
    op.execute("""CREATE TABLE workflow_execution_events (
        tenant_id uuid NOT NULL,
        execution_id uuid NOT NULL,
        seq bigint NOT NULL CHECK (seq > 0),
        event_type text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        payload_ciphertext text NOT NULL,
        payload_nonce text NOT NULL,
        payload_key_id uuid NOT NULL REFERENCES content_encryption_keys(key_id),
        PRIMARY KEY (execution_id, seq),
        FOREIGN KEY (tenant_id, execution_id) REFERENCES workflow_execution_runs(tenant_id,id)
    )""")
    op.execute("""CREATE TABLE workflow_execution_approvals (
        id text PRIMARY KEY,
        tenant_id uuid NOT NULL,
        execution_id uuid NOT NULL,
        node_id text NOT NULL,
        approver_user_id uuid NOT NULL REFERENCES users(user_id),
        status text NOT NULL DEFAULT 'pending' CHECK (status IN
            ('pending','decision_requested','approved','rejected','timeout','cancelled','execution_lost')),
        deadline timestamptz NOT NULL,
        requested_at timestamptz NOT NULL DEFAULT now(),
        requested_decision boolean,
        requested_by uuid REFERENCES users(user_id),
        decision_requested_at timestamptz,
        approved boolean,
        resolved_at timestamptz,
        FOREIGN KEY (tenant_id, execution_id) REFERENCES workflow_execution_runs(tenant_id,id),
        CHECK ((requested_decision IS NULL) = (requested_by IS NULL))
    )""")
    op.execute("""CREATE INDEX ix_workflow_history_source ON workflow_execution_runs
        (tenant_id,source_type,source_id,created_at DESC,id DESC)""")
    op.execute("""CREATE INDEX ix_workflow_history_status ON workflow_execution_runs
        (tenant_id,status,created_at DESC,id DESC)""")
    op.execute("""CREATE INDEX ix_workflow_approval_assignee ON workflow_execution_approvals
        (tenant_id,approver_user_id,status,execution_id)""")
    op.execute("""CREATE INDEX ix_workflow_approval_commands ON workflow_execution_approvals
        (execution_id,status)""")
    for table in ("workflow_execution_runs", "workflow_execution_events", "workflow_execution_approvals"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)""")


def downgrade():
    op.execute("DROP TABLE workflow_execution_approvals")
    op.execute("DROP TABLE workflow_execution_events")
    op.execute("DROP TABLE workflow_execution_runs")
