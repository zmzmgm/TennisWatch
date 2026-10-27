"""Worker dispatch receipts and runner completion/incident tracking."""
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = depends_on = None


def upgrade():
    op.execute("""CREATE TABLE public.scheduler_jobs (
        job_key TEXT PRIMARY KEY, command TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
        run_id TEXT, error_code TEXT, summary JSONB,
        alert_claimed_at TIMESTAMPTZ, alerted_at TIMESTAMPTZ,
        CHECK (command IN ('watch-slots','daily-reports','check-policies','send-outbox')),
        CHECK (status IN ('PENDING','RUNNING','SUCCESS','FAILED')))
    """)
    op.execute("CREATE INDEX scheduler_jobs_pending ON public.scheduler_jobs(command, requested_at) WHERE status IN ('PENDING','RUNNING')")
    op.execute('ALTER TABLE public.scheduler_jobs ENABLE ROW LEVEL SECURITY')
    op.execute('REVOKE ALL ON public.scheduler_jobs FROM anon, authenticated')
    op.execute('GRANT SELECT, INSERT, UPDATE ON public.scheduler_jobs TO service_role')
    op.execute("""CREATE FUNCTION public.reserve_scheduler_job(p_key TEXT, p_command TEXT)
    RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
    BEGIN
      PERFORM pg_advisory_xact_lock(73109321);
      IF EXISTS (SELECT 1 FROM scheduler_jobs WHERE command=p_command
          AND status IN ('PENDING','RUNNING') AND requested_at > now()-interval '45 minutes') THEN
        RETURN false;
      END IF;
      INSERT INTO scheduler_jobs(job_key, command) VALUES(p_key,p_command) ON CONFLICT DO NOTHING;
      RETURN FOUND;
    END $$""")
    op.execute("""CREATE FUNCTION public.claim_scheduler_incidents()
    RETURNS SETOF public.scheduler_jobs LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
      UPDATE scheduler_jobs SET alert_claimed_at=now()
      WHERE alerted_at IS NULL
        AND (alert_claimed_at IS NULL OR alert_claimed_at < now()-interval '5 minutes')
        AND (status='FAILED' OR (status IN ('PENDING','RUNNING') AND requested_at < now()-interval '45 minutes'))
      RETURNING *;
    $$""")
    op.execute("""CREATE FUNCTION public.daily_reports_due() RETURNS BOOLEAN
    LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
      SELECT EXISTS (SELECT 1 FROM daily_report_settings d JOIN telegram_users u ON u.id=d.user_id
        WHERE d.enabled AND u.enabled AND d.next_run_at <= now());
    $$""")
    for signature in ('reserve_scheduler_job(TEXT,TEXT)', 'claim_scheduler_incidents()', 'daily_reports_due()'):
        op.execute(f'REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC, anon, authenticated')
        op.execute(f'GRANT EXECUTE ON FUNCTION public.{signature} TO service_role')
    op.execute("NOTIFY pgrst, 'reload schema'")


def downgrade():
    for signature in ('reserve_scheduler_job(TEXT,TEXT)', 'claim_scheduler_incidents()', 'daily_reports_due()'):
        op.execute(f'DROP FUNCTION public.{signature}')
    op.execute('DROP TABLE public.scheduler_jobs')
