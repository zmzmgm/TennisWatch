"""Add user watches, conversation context and idempotent tool mutations.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE public.telegram_users ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_users_telegram_user_id ON public.telegram_users (telegram_user_id) WHERE telegram_user_id IS NOT NULL")
    op.execute("ALTER TABLE public.facilities ADD COLUMN IF NOT EXISTS region VARCHAR(120)")
    op.execute("ALTER TABLE public.facilities ADD COLUMN IF NOT EXISTS collection_status VARCHAR(24) NOT NULL DEFAULT 'UNVERIFIED'")
    # This identifier is the one verified by the existing read-only Gangseo adapter.
    op.execute("""
        UPDATE public.facilities AS f
        SET region = COALESCE(f.region, '서울 강서구'), collection_status = 'SUPPORTED'
        FROM public.reservation_sites AS s
        WHERE s.id = f.site_id AND s.key = 'gangseo_seoul'
          AND f.external_id = 'GANGSEO02:T:03:2'
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.user_watches (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES public.telegram_users(id),
            watch_type VARCHAR(24) NOT NULL CHECK (watch_type IN ('availability', 'opening')),
            criteria JSONB NOT NULL,
            criteria_hash VARCHAR(32) NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            monitoring_status VARCHAR(32) NOT NULL DEFAULT 'PENDING_ACTIVATION',
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_user_watch_criteria UNIQUE (user_id, watch_type, criteria_hash)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.user_watch_facilities (
            user_watch_id BIGINT NOT NULL REFERENCES public.user_watches(id) ON DELETE CASCADE,
            facility_id INTEGER NOT NULL REFERENCES public.facilities(id),
            PRIMARY KEY (user_watch_id, facility_id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.conversation_contexts (
            id BIGSERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES public.telegram_users(id) ON DELETE CASCADE,
            conversation_id VARCHAR(100) NOT NULL,
            last_intent VARCHAR(80),
            last_search_filters JSONB,
            active_subscription_id BIGINT REFERENCES public.user_watches(id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_conversation_context_owner UNIQUE (user_id, conversation_id)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.agent_tool_invocations (
            id BIGSERIAL PRIMARY KEY,
            update_id BIGINT NOT NULL REFERENCES public.telegram_updates(update_id) ON DELETE CASCADE,
            tool_call_id VARCHAR(200) NOT NULL,
            telegram_user_id BIGINT NOT NULL,
            tool_name VARCHAR(80) NOT NULL,
            args_hash VARCHAR(32) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'RUNNING',
            result JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT uq_tool_invocation_update_call UNIQUE (update_id, tool_call_id),
            CONSTRAINT uq_tool_invocation_update_action_args UNIQUE (update_id, tool_name, args_hash)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_watches_enabled ON public.user_watches (enabled, expires_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_conversation_contexts_expiry ON public.conversation_contexts (expires_at)")
    for table in ("user_watches", "user_watch_facilities", "conversation_contexts", "agent_tool_invocations"):
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')

    op.execute("""
        CREATE FUNCTION public.apply_user_watch_mutation(
            p_update_id BIGINT,
            p_tool_call_id TEXT,
            p_telegram_user_id BIGINT,
            p_chat_id TEXT,
            p_action TEXT,
            p_args JSONB,
            p_facility_ids INTEGER[] DEFAULT NULL
        ) RETURNS JSONB
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_user_id BIGINT;
            v_invocation public.agent_tool_invocations%ROWTYPE;
            v_args_hash TEXT;
            v_watch public.user_watches%ROWTYPE;
            v_watch_id BIGINT;
            v_watch_type TEXT;
            v_criteria JSONB;
            v_patch JSONB;
            v_criteria_hash TEXT;
            v_result JSONB;
            v_expires_at TIMESTAMPTZ;
            v_changed_facilities BOOLEAN := FALSE;
        BEGIN
            IF p_action NOT IN ('create_subscription', 'update_subscription', 'delete_subscription') THEN
                RETURN jsonb_build_object('ok', false, 'code', 'invalid_action');
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.telegram_updates
                WHERE update_id = p_update_id AND telegram_user_id = p_telegram_user_id
                  AND chat_id::TEXT = p_chat_id
            ) THEN
                RETURN jsonb_build_object('ok', false, 'code', 'unauthorized_update');
            END IF;

            SELECT id INTO v_user_id FROM public.telegram_users
            WHERE telegram_user_id = p_telegram_user_id FOR UPDATE;
            IF v_user_id IS NULL THEN
                SELECT id INTO v_user_id FROM public.telegram_users
                WHERE chat_id = p_chat_id FOR UPDATE;
                IF v_user_id IS NULL THEN
                    INSERT INTO public.telegram_users (chat_id, telegram_user_id, enabled)
                    VALUES (p_chat_id, p_telegram_user_id, TRUE) RETURNING id INTO v_user_id;
                ELSE
                    UPDATE public.telegram_users SET telegram_user_id = p_telegram_user_id, enabled = TRUE
                    WHERE id = v_user_id;
                END IF;
            ELSE
                UPDATE public.telegram_users SET enabled = TRUE WHERE id = v_user_id;
            END IF;

            v_args_hash := md5(p_action || ':' || p_args::TEXT);
            INSERT INTO public.agent_tool_invocations
                (update_id, tool_call_id, telegram_user_id, tool_name, args_hash)
            VALUES (p_update_id, p_tool_call_id, p_telegram_user_id, p_action, v_args_hash)
            ON CONFLICT DO NOTHING;

            SELECT * INTO v_invocation FROM public.agent_tool_invocations
            WHERE update_id = p_update_id
              AND (tool_call_id = p_tool_call_id OR (tool_name = p_action AND args_hash = v_args_hash))
            ORDER BY id LIMIT 1 FOR UPDATE;
            IF NOT FOUND THEN
                RETURN jsonb_build_object('ok', false, 'code', 'idempotency_error');
            END IF;
            IF v_invocation.telegram_user_id <> p_telegram_user_id THEN
                RETURN jsonb_build_object('ok', false, 'code', 'idempotency_conflict');
            END IF;
            IF v_invocation.args_hash <> v_args_hash OR v_invocation.tool_name <> p_action THEN
                RETURN jsonb_build_object('ok', false, 'code', 'idempotency_conflict');
            END IF;
            IF v_invocation.result IS NOT NULL THEN
                RETURN v_invocation.result;
            END IF;

            IF p_action = 'create_subscription' THEN
                IF COALESCE(array_length(p_facility_ids, 1), 0) = 0 THEN
                    v_result := jsonb_build_object('ok', false, 'code', 'unsupported');
                ELSE
                    v_watch_type := p_args->>'watch_type';
                    v_criteria := jsonb_strip_nulls(p_args - 'watch_type');
                    v_criteria_hash := md5(v_criteria::TEXT);
                    v_expires_at := CASE WHEN v_criteria ? 'play_date'
                        THEN (((v_criteria->>'play_date')::DATE + 1)::TIMESTAMP AT TIME ZONE 'Asia/Seoul')
                        ELSE NULL END;
                    INSERT INTO public.user_watches
                        (user_id, watch_type, criteria, criteria_hash, enabled, monitoring_status, expires_at)
                    VALUES (v_user_id, v_watch_type, v_criteria, v_criteria_hash, TRUE, 'PENDING_ACTIVATION', v_expires_at)
                    ON CONFLICT (user_id, watch_type, criteria_hash) DO UPDATE
                        SET enabled = TRUE,
                            monitoring_status = CASE WHEN user_watches.enabled THEN user_watches.monitoring_status ELSE 'PENDING_ACTIVATION' END,
                            expires_at = EXCLUDED.expires_at,
                            updated_at = now()
                    RETURNING id INTO v_watch_id;
                    DELETE FROM public.user_watch_facilities WHERE user_watch_id = v_watch_id;
                    INSERT INTO public.user_watch_facilities (user_watch_id, facility_id)
                    SELECT v_watch_id, facility_id FROM unnest(p_facility_ids) AS items(facility_id)
                    ON CONFLICT DO NOTHING;
                    v_result := jsonb_build_object('ok', true, 'data', jsonb_build_object(
                        'subscription_id', v_watch_id,
                        'watch_type', v_watch_type,
                        'monitoring_status', 'PENDING_ACTIVATION',
                        'facility_count', array_length(p_facility_ids, 1),
                        'criteria', v_criteria
                    ));
                END IF;
            ELSIF p_action = 'update_subscription' THEN
                SELECT * INTO v_watch FROM public.user_watches
                WHERE id = (p_args->>'subscription_id')::BIGINT AND user_id = v_user_id FOR UPDATE;
                IF NOT FOUND THEN
                    v_result := jsonb_build_object('ok', false, 'code', 'not_found');
                ELSE
                    v_patch := COALESCE(p_args->'patch', '{}'::JSONB);
                    v_criteria := v_watch.criteria || (v_patch - 'watch_type' - 'enabled');
                    IF v_patch ? 'play_date' THEN v_criteria := v_criteria - 'weekdays'; END IF;
                    IF v_patch ? 'weekdays' THEN v_criteria := v_criteria - 'play_date'; END IF;
                    IF v_patch ? 'region' AND NOT (v_patch ? 'facility') THEN v_criteria := v_criteria - 'facility'; END IF;
                    IF v_patch ? 'facility' AND NOT (v_patch ? 'region') THEN v_criteria := v_criteria - 'region'; END IF;
                    v_watch_type := COALESCE(v_patch->>'watch_type', v_watch.watch_type);
                    v_criteria_hash := md5(v_criteria::TEXT);
                    v_expires_at := CASE WHEN v_criteria ? 'play_date'
                        THEN (((v_criteria->>'play_date')::DATE + 1)::TIMESTAMP AT TIME ZONE 'Asia/Seoul')
                        ELSE NULL END;
                    UPDATE public.user_watches SET
                        watch_type = v_watch_type,
                        criteria = v_criteria,
                        criteria_hash = v_criteria_hash,
                        enabled = COALESCE((v_patch->>'enabled')::BOOLEAN, enabled),
                        monitoring_status = CASE WHEN COALESCE((v_patch->>'enabled')::BOOLEAN, enabled)
                            THEN 'PENDING_ACTIVATION' ELSE 'DISABLED' END,
                        expires_at = v_expires_at,
                        updated_at = now()
                    WHERE id = v_watch.id;
                    IF p_facility_ids IS NOT NULL THEN
                        DELETE FROM public.user_watch_facilities WHERE user_watch_id = v_watch.id;
                        INSERT INTO public.user_watch_facilities (user_watch_id, facility_id)
                        SELECT v_watch.id, facility_id FROM unnest(p_facility_ids) AS items(facility_id)
                        ON CONFLICT DO NOTHING;
                        v_changed_facilities := TRUE;
                    END IF;
                    v_result := jsonb_build_object('ok', true, 'data', jsonb_build_object(
                        'subscription_id', v_watch.id,
                        'watch_type', v_watch_type,
                        'enabled', COALESCE((v_patch->>'enabled')::BOOLEAN, v_watch.enabled),
                        'monitoring_status', CASE WHEN COALESCE((v_patch->>'enabled')::BOOLEAN, v_watch.enabled)
                            THEN 'PENDING_ACTIVATION' ELSE 'DISABLED' END,
                        'facility_count', CASE WHEN v_changed_facilities THEN COALESCE(array_length(p_facility_ids, 1), 0)
                            ELSE (SELECT count(*) FROM public.user_watch_facilities WHERE user_watch_id = v_watch.id) END,
                        'criteria', v_criteria
                    ));
                END IF;
            ELSE
                UPDATE public.user_watches SET enabled = FALSE, monitoring_status = 'DISABLED', updated_at = now()
                WHERE id = (p_args->>'subscription_id')::BIGINT AND user_id = v_user_id
                RETURNING * INTO v_watch;
                IF NOT FOUND THEN
                    v_result := jsonb_build_object('ok', false, 'code', 'not_found');
                ELSE
                    v_result := jsonb_build_object('ok', true, 'data', jsonb_build_object(
                        'subscription_id', v_watch.id, 'watch_type', v_watch.watch_type,
                        'enabled', false, 'monitoring_status', 'DISABLED', 'criteria', v_watch.criteria
                    ));
                END IF;
            END IF;

            UPDATE public.agent_tool_invocations
            SET status = CASE WHEN v_result->>'ok' = 'true' THEN 'SUCCEEDED' ELSE 'FAILED' END,
                result = v_result, completed_at = now()
            WHERE id = v_invocation.id;
            RETURN v_result;
        END
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.apply_user_watch_mutation(BIGINT, TEXT, BIGINT, TEXT, TEXT, JSONB, INTEGER[]) FROM PUBLIC, anon, authenticated")
    op.execute("GRANT EXECUTE ON FUNCTION public.apply_user_watch_mutation(BIGINT, TEXT, BIGINT, TEXT, TEXT, JSONB, INTEGER[]) TO service_role")


def downgrade():
    op.execute("DROP FUNCTION IF EXISTS public.apply_user_watch_mutation(BIGINT, TEXT, BIGINT, TEXT, TEXT, JSONB, INTEGER[])")
    for table in ("agent_tool_invocations", "conversation_contexts", "user_watch_facilities", "user_watches"):
        op.execute(f'DROP TABLE IF EXISTS public."{table}" CASCADE')
    op.execute("ALTER TABLE public.facilities DROP COLUMN IF EXISTS collection_status")
    op.execute("ALTER TABLE public.facilities DROP COLUMN IF EXISTS region")
    op.execute("DROP INDEX IF EXISTS public.uq_telegram_users_telegram_user_id")
    op.execute("ALTER TABLE public.telegram_users DROP COLUMN IF EXISTS telegram_user_id")
