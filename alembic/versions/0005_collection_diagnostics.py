"""Track each facility collection and its safe failure code and elapsed time."""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.facilities ADD COLUMN IF NOT EXISTS last_collected_at TIMESTAMPTZ")
    op.execute("ALTER TABLE public.collection_runs ADD COLUMN IF NOT EXISTS facility_id INTEGER REFERENCES public.facilities(id)")
    op.execute("ALTER TABLE public.collection_runs ADD COLUMN IF NOT EXISTS requested_dates JSONB")
    op.execute("ALTER TABLE public.collection_runs ADD COLUMN IF NOT EXISTS duration_ms INTEGER")
    op.execute("ALTER TABLE public.collection_runs ADD COLUMN IF NOT EXISTS error_code VARCHAR(60)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_collection_runs_facility_started ON public.collection_runs (facility_id, started_at DESC)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS public.ix_collection_runs_facility_started")
    for column in ("error_code", "duration_ms", "requested_dates", "facility_id"):
        op.execute(f"ALTER TABLE public.collection_runs DROP COLUMN IF EXISTS {column}")
    op.execute("ALTER TABLE public.facilities DROP COLUMN IF EXISTS last_collected_at")
