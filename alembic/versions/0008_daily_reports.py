"""Add opt-in daily snapshots and per-user/day outbox uniqueness."""
from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE IF NOT EXISTS public.daily_report_settings (
        user_id INTEGER PRIMARY KEY REFERENCES public.telegram_users(id) ON DELETE CASCADE,
        minute_of_day INTEGER NOT NULL CHECK (minute_of_day >= 0 AND minute_of_day < 1440),
        enabled BOOLEAN NOT NULL DEFAULT true,
        revision INTEGER NOT NULL DEFAULT 1,
        next_run_at TIMESTAMPTZ NOT NULL,
        last_update_id BIGINT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )''')
    op.execute('ALTER TABLE public.daily_report_settings ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON public.daily_report_settings FROM anon, authenticated')
    op.execute('GRANT SELECT ON public.daily_report_settings TO service_role')
    op.execute('ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS report_date DATE')
    op.execute('ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS report_revision INTEGER')
    op.execute('''DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.notifications'::regclass
            AND conname='uq_notification_daily_user_date') THEN
            ALTER TABLE public.notifications ADD CONSTRAINT uq_notification_daily_user_date UNIQUE(user_id,kind,report_date);
        END IF;
    END $$''')
    op.execute('''CREATE FUNCTION public.manage_daily_report(
        p_update_id BIGINT, p_user_id BIGINT, p_chat_id TEXT, p_action TEXT, p_minute INTEGER DEFAULT NULL
    ) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
    DECLARE
        v_user INTEGER;
        v_row public.daily_report_settings%ROWTYPE;
        v_due TIMESTAMPTZ;
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM public.telegram_updates WHERE update_id=p_update_id
            AND telegram_user_id=p_user_id AND chat_id::TEXT=p_chat_id)
            OR p_chat_id <> p_user_id::TEXT THEN
            RETURN jsonb_build_object('ok',false,'code','unauthorized');
        END IF;
        SELECT id INTO v_user FROM public.telegram_users WHERE telegram_user_id=p_user_id
            AND chat_id=p_chat_id AND enabled FOR UPDATE;
        IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'code','not_found'); END IF;
        IF p_action NOT IN ('set','get','disable') OR p_action IS NULL THEN
            RETURN jsonb_build_object('ok',false,'code','invalid_input');
        END IF;
        SELECT * INTO v_row FROM public.daily_report_settings WHERE user_id=v_user FOR UPDATE;
        IF p_action <> 'get' THEN
            IF v_row.last_update_id IS NOT NULL AND p_update_id < v_row.last_update_id THEN
                RETURN jsonb_build_object('ok',false,'code','stale_update');
            END IF;
            IF v_row.last_update_id IS DISTINCT FROM p_update_id THEN
                IF p_action='set' THEN
                    IF p_minute IS NULL OR p_minute < 0 OR p_minute > 1439 THEN
                        RETURN jsonb_build_object('ok',false,'code','invalid_input');
                    END IF;
                    v_due := ((now() AT TIME ZONE 'Asia/Seoul')::DATE::TIMESTAMP
                        + make_interval(mins=>p_minute)) AT TIME ZONE 'Asia/Seoul';
                    IF v_due <= now() THEN v_due := v_due + interval '1 day'; END IF;
                    INSERT INTO public.daily_report_settings(user_id,minute_of_day,next_run_at,last_update_id)
                        VALUES(v_user,p_minute,v_due,p_update_id)
                    ON CONFLICT(user_id) DO UPDATE SET
                        minute_of_day=EXCLUDED.minute_of_day, enabled=true,
                        revision=CASE WHEN daily_report_settings.enabled AND daily_report_settings.minute_of_day=p_minute
                            THEN daily_report_settings.revision ELSE daily_report_settings.revision+1 END,
                        next_run_at=CASE WHEN daily_report_settings.enabled AND daily_report_settings.minute_of_day=p_minute
                            THEN daily_report_settings.next_run_at ELSE EXCLUDED.next_run_at END,
                        last_update_id=p_update_id,updated_at=now();
                ELSE
                    INSERT INTO public.daily_report_settings(user_id,minute_of_day,enabled,next_run_at,last_update_id)
                        VALUES(v_user,0,false,now(),p_update_id)
                    ON CONFLICT(user_id) DO UPDATE SET enabled=false,
                        revision=daily_report_settings.revision+CASE WHEN daily_report_settings.enabled THEN 1 ELSE 0 END,
                        last_update_id=p_update_id,updated_at=now();
                END IF;
            END IF;
            SELECT * INTO v_row FROM public.daily_report_settings WHERE user_id=v_user;
        END IF;
        RETURN jsonb_build_object('ok',true,'data',jsonb_build_object(
            'configured',v_row.user_id IS NOT NULL,'enabled',COALESCE(v_row.enabled,false),
            'local_time',CASE WHEN v_row.user_id IS NULL THEN NULL ELSE
                lpad((v_row.minute_of_day/60)::TEXT,2,'0')||':'||lpad((v_row.minute_of_day%60)::TEXT,2,'0') END,
            'timezone','Asia/Seoul','next_run_at',v_row.next_run_at,
            'scope','existing_availability_watches','horizon_days',7,
            'execution_note','예약 실행은 지연될 수 있으며 정기 작업 배포·실행 확인이 필요합니다.'));
    END $$''')
    op.execute('REVOKE ALL ON FUNCTION public.manage_daily_report(BIGINT,BIGINT,TEXT,TEXT,INTEGER) FROM PUBLIC,anon,authenticated')
    op.execute('GRANT EXECUTE ON FUNCTION public.manage_daily_report(BIGINT,BIGINT,TEXT,TEXT,INTEGER) TO service_role')


def downgrade():
    op.execute('DROP FUNCTION public.manage_daily_report(BIGINT,BIGINT,TEXT,TEXT,INTEGER)')
    op.drop_constraint('uq_notification_daily_user_date','notifications',type_='unique')
    op.drop_column('notifications','report_revision')
    op.drop_column('notifications','report_date')
    op.drop_table('daily_report_settings')
