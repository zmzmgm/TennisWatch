"""Add a durable, idempotent Telegram webhook inbox.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.telegram_updates (
            update_id BIGINT PRIMARY KEY,
            telegram_user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            message_text TEXT NOT NULL CHECK (char_length(message_text) BETWEEN 1 AND 4096),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'processed', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            lease_until TIMESTAMPTZ,
            processed_at TIMESTAMPTZ,
            last_error TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_telegram_updates_pending ON public.telegram_updates (next_attempt_at, received_at) WHERE status = 'pending'")
    op.execute("ALTER TABLE public.telegram_updates ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE FUNCTION public.claim_telegram_updates(p_limit INTEGER DEFAULT 10)
        RETURNS SETOF public.telegram_updates
        LANGUAGE SQL
        SECURITY DEFINER
        SET search_path = public
        AS $$
            WITH expired AS (
                UPDATE public.telegram_updates
                SET status = 'failed', lease_until = NULL,
                    last_error = COALESCE(last_error, 'processing lease expired after final attempt')
                WHERE status = 'processing' AND lease_until < now() AND attempts >= 5
            ), picked AS (
                SELECT update_id
                FROM public.telegram_updates
                WHERE attempts < 5
                  AND ((status = 'pending' AND next_attempt_at <= now())
                       OR (status = 'processing' AND lease_until < now()))
                ORDER BY received_at
                FOR UPDATE SKIP LOCKED
                LIMIT LEAST(GREATEST(p_limit, 1), 50)
            )
            UPDATE public.telegram_updates AS u
            SET status = 'processing', attempts = attempts + 1,
                lease_until = now() + interval '2 minutes'
            FROM picked
            WHERE u.update_id = picked.update_id
            RETURNING u.*
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.claim_telegram_updates(INTEGER) FROM PUBLIC, anon, authenticated")
    op.execute("GRANT EXECUTE ON FUNCTION public.claim_telegram_updates(INTEGER) TO service_role")


def downgrade():
    op.execute("DROP FUNCTION IF EXISTS public.claim_telegram_updates(INTEGER)")
    op.execute("DROP TABLE IF EXISTS public.telegram_updates")
