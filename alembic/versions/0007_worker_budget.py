"""Persist LLM budgets, cached replies and fenced webhook processing leases."""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.telegram_updates ADD COLUMN IF NOT EXISTS lease_token VARCHAR(36)")
    op.execute("ALTER TABLE public.telegram_updates ADD COLUMN IF NOT EXISTS response_text TEXT")
    op.execute("""CREATE TABLE IF NOT EXISTS public.llm_usage (
        id BIGSERIAL PRIMARY KEY,
        update_id BIGINT NOT NULL REFERENCES public.telegram_updates(update_id) ON DELETE CASCADE,
        telegram_user_id BIGINT NOT NULL,
        request_key VARCHAR(36) NOT NULL UNIQUE,
        model_name VARCHAR(160) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'RESERVED',
        reserved_tokens INTEGER NOT NULL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        error_code VARCHAR(60),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("ALTER TABLE public.llm_usage ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_usage_created ON public.llm_usage (created_at, telegram_user_id)")
    op.execute("""CREATE OR REPLACE FUNCTION public.claim_telegram_updates(p_limit INTEGER DEFAULT 1)
        RETURNS SETOF public.telegram_updates LANGUAGE SQL SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        WITH expired AS (
            UPDATE public.telegram_updates SET status='failed', lease_until=NULL, lease_token=NULL,
                last_error=COALESCE(last_error, 'PROCESSING_EXHAUSTED')
            WHERE status='processing' AND lease_until < now() AND attempts >= 5
        ), picked AS (
            SELECT update_id FROM public.telegram_updates WHERE attempts < 5
              AND ((status='pending' AND next_attempt_at <= now()) OR (status='processing' AND lease_until < now()))
            ORDER BY received_at FOR UPDATE SKIP LOCKED LIMIT LEAST(GREATEST(p_limit, 1), 1)
        ) UPDATE public.telegram_updates AS u SET status='processing', attempts=attempts+1,
            lease_until=now()+interval '2 minutes', lease_token=gen_random_uuid()::TEXT
          FROM picked WHERE u.update_id=picked.update_id RETURNING u.*
        $$""")
    op.execute("""CREATE FUNCTION public.reserve_llm_call(
        p_user_id BIGINT, p_update_id BIGINT, p_request_key TEXT, p_model TEXT,
        p_reserved_tokens INTEGER, p_user_limit INTEGER DEFAULT 20,
        p_global_limit INTEGER DEFAULT 100, p_token_budget INTEGER DEFAULT 100000
    ) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    DECLARE
        v_start TIMESTAMPTZ := date_trunc('day', now() AT TIME ZONE 'Asia/Seoul') AT TIME ZONE 'Asia/Seoul';
        v_global BIGINT;
        v_user BIGINT;
        v_tokens BIGINT;
        v_id BIGINT;
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM public.telegram_updates WHERE update_id=p_update_id AND telegram_user_id=p_user_id) THEN
            RETURN jsonb_build_object('ok', false, 'code', 'UNAUTHORIZED_UPDATE');
        END IF;
        IF p_reserved_tokens < 1 OR p_reserved_tokens > 100000 OR length(p_request_key) > 36 OR length(p_model) > 160 THEN
            RETURN jsonb_build_object('ok', false, 'code', 'INVALID_RESERVATION');
        END IF;
        PERFORM pg_advisory_xact_lock(907170001);
        SELECT id INTO v_id FROM public.llm_usage WHERE request_key=p_request_key
            AND update_id=p_update_id AND telegram_user_id=p_user_id;
        IF FOUND THEN RETURN jsonb_build_object('ok', true, 'reservation_id', v_id); END IF;
        SELECT count(*), count(*) FILTER (WHERE telegram_user_id=p_user_id),
            COALESCE(sum(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                        THEN input_tokens::BIGINT + output_tokens ELSE reserved_tokens END), 0)
            INTO v_global, v_user, v_tokens FROM public.llm_usage WHERE created_at >= v_start;
        IF v_global >= LEAST(GREATEST(p_global_limit,1),1000)
           OR v_user >= LEAST(GREATEST(p_user_limit,1),100)
           OR v_tokens+p_reserved_tokens > LEAST(GREATEST(p_token_budget,1),1000000) THEN
            RETURN jsonb_build_object('ok', false, 'code', 'LLM_BUDGET_EXCEEDED');
        END IF;
        INSERT INTO public.llm_usage(update_id, telegram_user_id, request_key, model_name, reserved_tokens)
            VALUES(p_update_id,p_user_id,p_request_key,p_model,p_reserved_tokens) RETURNING id INTO v_id;
        RETURN jsonb_build_object('ok', true, 'reservation_id', v_id);
    END $$""")
    op.execute("REVOKE ALL ON FUNCTION public.reserve_llm_call(BIGINT,BIGINT,TEXT,TEXT,INTEGER,INTEGER,INTEGER,INTEGER) FROM PUBLIC, anon, authenticated")
    op.execute("GRANT EXECUTE ON FUNCTION public.reserve_llm_call(BIGINT,BIGINT,TEXT,TEXT,INTEGER,INTEGER,INTEGER,INTEGER) TO service_role")
    op.execute("REVOKE ALL ON FUNCTION public.claim_telegram_updates(INTEGER) FROM PUBLIC, anon, authenticated")
    op.execute("GRANT EXECUTE ON FUNCTION public.claim_telegram_updates(INTEGER) TO service_role")
    op.execute("""CREATE FUNCTION public.worker_readiness() RETURNS BOOLEAN
        LANGUAGE SQL SECURITY INVOKER SET search_path = pg_catalog, public AS $$
        SELECT current_user = 'service_role'
          AND (SELECT count(lease_token)+count(response_text) FROM public.telegram_updates WHERE false) = 0
          AND (SELECT count(id) FROM public.llm_usage WHERE false) = 0
          AND (SELECT count(id) FROM public.user_watches WHERE false) = 0
        $$""")
    op.execute("REVOKE ALL ON FUNCTION public.worker_readiness() FROM PUBLIC, anon, authenticated")
    op.execute("GRANT EXECUTE ON FUNCTION public.worker_readiness() TO service_role")


def downgrade():
    raise RuntimeError("0007 이전으로 되돌리려면 Worker를 중지하고 사용량 기록을 백업한 뒤 별도 이전이 필요합니다.")
