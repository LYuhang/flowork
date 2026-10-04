"""Configure resident worker count and asynchronous capacity per worker."""
from alembic import op

revision = "159"
down_revision = "158"
branch_labels = None
depends_on = None


def upgrade():
    # Bootstrap uses current ORM metadata; constraints are installed explicitly.
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS worker_count integer NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS worker_concurrency integer NOT NULL DEFAULT 4")
    op.execute("UPDATE deployments SET worker_count = GREATEST(1, cpu_millis / 1000)")
    op.execute("ALTER TABLE deployments ADD CONSTRAINT ck_deployment_worker_count CHECK (worker_count BETWEEN 1 AND 256)")
    op.execute("ALTER TABLE deployments ADD CONSTRAINT ck_deployment_worker_concurrency CHECK (worker_concurrency BETWEEN 1 AND 64)")


def downgrade():
    op.execute("ALTER TABLE deployments DROP COLUMN worker_concurrency")
    op.execute("ALTER TABLE deployments DROP COLUMN worker_count")
