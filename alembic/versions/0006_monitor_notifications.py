"""Add fenced notification leases, versioned opening reminders and policy snapshots."""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS lease_token VARCHAR(36)")
    op.execute("ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS schedule_opens_at TIMESTAMPTZ")
    op.execute("ALTER TABLE public.reservation_policies ADD COLUMN IF NOT EXISTS last_checked_value TEXT")
    op.execute("ALTER TABLE public.notifications DROP CONSTRAINT IF EXISTS uq_notification_schedule_user_kind")
    op.execute("ALTER TABLE public.notifications ADD CONSTRAINT uq_notification_schedule_user_kind UNIQUE (schedule_id, user_id, kind, schedule_opens_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_notifications_due ON public.notifications (status, next_attempt_at)")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM public.notifications WHERE schedule_id IS NOT NULL
                   GROUP BY schedule_id, user_id, kind HAVING count(*) > 1) THEN
            RAISE EXCEPTION '알림 버전별 이력이 있어 이전 고유 제약으로 축소할 수 없습니다. 백업 후 데이터 이전이 필요합니다.';
        END IF;
    END $$""")
    op.execute("DROP INDEX IF EXISTS public.ix_notifications_due")
    op.execute("ALTER TABLE public.notifications DROP CONSTRAINT uq_notification_schedule_user_kind")
    op.execute("ALTER TABLE public.notifications ADD CONSTRAINT uq_notification_schedule_user_kind UNIQUE (schedule_id, user_id, kind)")
    op.execute("ALTER TABLE public.notifications DROP COLUMN schedule_opens_at")
    op.execute("ALTER TABLE public.notifications DROP COLUMN lease_token")
    op.execute("ALTER TABLE public.reservation_policies DROP COLUMN last_checked_value")
