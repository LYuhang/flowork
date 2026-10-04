"""Separate a user's installation choice from Skill ownership and access."""
from alembic import op

revision = "165"
down_revision = "164"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS user_skill_installations (
        user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        skill_id uuid NOT NULL REFERENCES skills(skill_id) ON DELETE CASCADE,
        installed_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, skill_id)
    )""")
    # Preserve existing creators' installation choices without opting recipients in.
    op.execute("""INSERT INTO user_skill_installations (user_id, skill_id)
                  SELECT user_id, skill_id FROM skills WHERE deleted_at IS NULL""")
    op.execute("ALTER TABLE user_skill_installations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_skill_installations FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY personal_skill_installations ON user_skill_installations
        USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)""")


def downgrade():
    op.execute("DROP TABLE user_skill_installations")
