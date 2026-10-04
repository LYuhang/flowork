"""Include custom Skill roots in recipient-scoped sharing discovery.

Revision ID: 161
Revises: 160
"""
from alembic import op

revision = "161"
down_revision = "160"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_shared_resource_projection_type", "shared_resource_projections", type_="check")
    op.create_check_constraint(
        "ck_shared_resource_projection_type", "shared_resource_projections",
        "resource_type IN ('workflow','task','deployment','knowledge_base','skill_installation')",
    )


def downgrade():
    # Downgrade refuses incompatible live shares rather than deleting grants.
    op.drop_constraint("ck_shared_resource_projection_type", "shared_resource_projections", type_="check")
    op.create_check_constraint(
        "ck_shared_resource_projection_type", "shared_resource_projections",
        "resource_type IN ('workflow','task','deployment','knowledge_base')",
    )
