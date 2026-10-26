"""주간 일괄 오픈 정책, 대상 이용기간 및 정책 출처 점검 이력."""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.reservation_policies ADD COLUMN IF NOT EXISTS rule_type VARCHAR(24) NOT NULL DEFAULT 'rolling_days'")
    op.execute("ALTER TABLE public.reservation_policies ADD COLUMN IF NOT EXISTS open_weekday INTEGER")
    op.execute("ALTER TABLE public.reservation_policies ADD COLUMN IF NOT EXISTS window_start_days INTEGER")
    op.execute("ALTER TABLE public.reservation_policies ADD COLUMN IF NOT EXISTS window_days INTEGER")
    op.execute("ALTER TABLE public.reservation_policies ADD COLUMN IF NOT EXISTS source_digest VARCHAR(64)")
    op.execute("ALTER TABLE public.reservation_policies ADD COLUMN IF NOT EXISTS source_checked_at TIMESTAMPTZ")
    op.execute("ALTER TABLE public.reservation_policies ADD COLUMN IF NOT EXISTS source_check_error VARCHAR(60)")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.reservation_policies'::regclass AND conname='ck_policy_rule') THEN
        ALTER TABLE public.reservation_policies ADD CONSTRAINT ck_policy_rule CHECK (
        (rule_type = 'rolling_days' AND open_days_before BETWEEN 0 AND 365) OR
        (rule_type = 'weekly_batch' AND open_days_before = 0 AND open_weekday IS NOT NULL
         AND open_weekday BETWEEN 1 AND 7 AND window_start_days IS NOT NULL
         AND window_start_days BETWEEN 1 AND 7 AND window_days IS NOT NULL AND window_days = 7));
        END IF; END $$""")
    op.execute("ALTER TABLE public.reservation_schedules ADD COLUMN IF NOT EXISTS play_date_end DATE")
    op.execute("UPDATE public.reservation_schedules SET play_date_end = play_date WHERE play_date_end IS NULL")
    op.execute("""CREATE TABLE IF NOT EXISTS public.policy_check_runs (
        id SERIAL PRIMARY KEY, facility_id INTEGER NOT NULL REFERENCES public.facilities(id),
        started_at TIMESTAMPTZ NOT NULL, duration_ms INTEGER NOT NULL,
        status VARCHAR(24) NOT NULL, error_code VARCHAR(60))""")
    op.execute("ALTER TABLE public.policy_check_runs ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON public.policy_check_runs FROM anon, authenticated")
    op.execute("GRANT SELECT ON public.policy_check_runs TO service_role")
    op.execute("NOTIFY pgrst, 'reload schema'")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.reservation_policies WHERE rule_type <> 'rolling_days') THEN
            RAISE EXCEPTION '주간 정책이 있어 자동 축소할 수 없습니다.';
        END IF;
    END $$""")
    op.execute("DROP TABLE public.policy_check_runs")
    op.execute("ALTER TABLE public.reservation_schedules DROP COLUMN play_date_end")
    op.execute("ALTER TABLE public.reservation_policies DROP CONSTRAINT ck_policy_rule")
    for field in ('source_check_error','source_checked_at','source_digest','window_days',
                  'window_start_days','open_weekday','rule_type'):
        op.execute(f"ALTER TABLE public.reservation_policies DROP COLUMN {field}")
