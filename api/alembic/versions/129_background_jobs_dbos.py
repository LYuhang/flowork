"""Rename the runtime-specific task delivery identifier.

Revision ID: 129
Revises: 128
"""

from alembic import op


revision = "129"
down_revision = "128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Migration 001 uses current SQLAlchemy metadata for fresh installs, so a
    # newly created database already has ``background_job_id``. Existing
    # deployments at revision 128 still have ``celery_id``. Support both paths.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'tasks'
                  AND column_name = 'celery_id'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'tasks'
                  AND column_name = 'background_job_id'
            ) THEN
                ALTER TABLE tasks RENAME COLUMN celery_id TO background_job_id;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE deployment_invocations
            ADD COLUMN private_ciphertext text NULL,
            ADD COLUMN private_nonce text NULL,
            ADD COLUMN private_key_id uuid NULL
                REFERENCES content_encryption_keys(key_id) ON DELETE RESTRICT
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE deployment_invocations
            DROP COLUMN private_key_id,
            DROP COLUMN private_nonce,
            DROP COLUMN private_ciphertext
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'tasks'
                  AND column_name = 'background_job_id'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'tasks'
                  AND column_name = 'celery_id'
            ) THEN
                ALTER TABLE tasks RENAME COLUMN background_job_id TO celery_id;
            END IF;
        END $$
        """
    )
