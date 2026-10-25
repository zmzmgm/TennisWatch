"""Enable RLS on application tables so public Data API roles cannot read them.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = (
    "reservation_sites",
    "facilities",
    "courts",
    "reservation_policies",
    "policy_changes",
    "reservation_schedules",
    "telegram_users",
    "subscriptions",
    "availability_slots",
    "availability_events",
    "notifications",
    "collection_runs",
)


def upgrade():
    for table in TABLES:
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')


def downgrade():
    for table in reversed(TABLES):
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
