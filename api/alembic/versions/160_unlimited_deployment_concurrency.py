"""Allow an explicitly unlimited per-worker concurrency, defaulting to -1."""
from alembic import op

revision = "160"
down_revision = "159"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE deployments DROP CONSTRAINT ck_deployment_worker_concurrency")
    op.execute("ALTER TABLE deployments ADD CONSTRAINT ck_deployment_worker_concurrency CHECK (worker_concurrency = -1 OR worker_concurrency BETWEEN 1 AND 64)")
    op.execute("ALTER TABLE deployments ALTER COLUMN worker_concurrency SET DEFAULT -1")


def downgrade():
    op.execute("UPDATE deployments SET worker_concurrency = 4 WHERE worker_concurrency = -1")
    op.execute("ALTER TABLE deployments ALTER COLUMN worker_concurrency SET DEFAULT 4")
    op.execute("ALTER TABLE deployments DROP CONSTRAINT ck_deployment_worker_concurrency")
    op.execute("ALTER TABLE deployments ADD CONSTRAINT ck_deployment_worker_concurrency CHECK (worker_concurrency BETWEEN 1 AND 64)")
