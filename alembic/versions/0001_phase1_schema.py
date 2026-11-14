"""create phase 1 business data model

Revision ID: 0001_phase1_schema
Revises:
"""

from alembic import op
from enterprise_business_agent import models  # noqa: F401
from enterprise_business_agent.db import Base

revision = "0001_phase1_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
