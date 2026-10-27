from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import Mock

from sqlalchemy import select

from tenniswatch import scheduled_jobs as jobs
from tenniswatch.collection import build_collection_plan
from tenniswatch.models import SchedulerJob, CollectionRun, DailyReportSetting
from test_daily_reports import db, NOW


def receipt(factory, key='watch:one', command='watch-slots'):
    with factory.begin() as s:
        s.add(SchedulerJob(job_key=key, command=command))


@contextmanager
def lock(*args):
    yield True


def test_claim_requires_matching_command_and_is_once(db):
    receipt(db)
    assert not jobs.claim(db, 'watch:one', 'daily-reports', '1')
    assert jobs.claim(db, 'watch:one', 'watch-slots', '1')
    assert not jobs.claim(db, 'watch:one', 'watch-slots', '2')
    assert not jobs.claim(db, 'missing', 'watch-slots', '3')


def test_success_receipt_and_duplicate_does_not_execute(db, monkeypatch):
    receipt(db)
    execute = Mock(return_value={'runs': 1, 'records': 10, 'errors': 0})
    monkeypatch.setattr(jobs, 'execute', execute)
    monkeypatch.setattr(jobs, 'advisory_job_lock', lock)
    assert jobs.run(db, 'watch:one', 'watch-slots', '1') == 0
    assert jobs.run(db, 'watch:one', 'watch-slots', '1') == 0
    execute.assert_called_once()
    with db() as s:
        row = s.get(SchedulerJob, 'watch:one')
        assert row.status == 'SUCCESS' and row.finished_at and row.run_id == '1'
        assert row.summary['records'] == 10


def test_exception_record_is_safe_and_failed(db, monkeypatch):
    receipt(db)
    monkeypatch.setattr(jobs, 'execute', Mock(side_effect=RuntimeError('secret-value')))
    monkeypatch.setattr(jobs, 'advisory_job_lock', lock)
    assert jobs.run(db, 'watch:one', 'watch-slots', '1') == 1
    with db() as s:
        row = s.get(SchedulerJob, 'watch:one')
        assert row.status == 'FAILED' and row.error_code == 'JOB_FAILED'
        assert row.summary is None


def test_daily_refreshes_before_reporting_even_if_partial(db, monkeypatch):
    order = []
    monkeypatch.setattr(jobs, 'check_freshness', lambda *a: {'unverified_dates': 0, 'stale_dates': 0})
    def collect(*args, **kwargs):
        assert kwargs['report_only'] and kwargs['activate_watches']
        assert args[2] == 6
        order.append('collect')
        return 1, 3, 1
    monkeypatch.setattr(jobs, 'collect_targets', collect)
    monkeypatch.setattr(jobs, 'enqueue_daily_reports', lambda factory: order.append('report') or 1)
    monkeypatch.setattr(jobs, '_deliver', lambda factory: order.append('deliver') or (1, 0))
    result = jobs.execute(db, 'daily-reports')
    assert order == ['collect', 'report', 'deliver']
    assert result['errors'] == 1 and result['queued'] == 1


def test_watch_never_enqueues_daily_report(db, monkeypatch):
    monkeypatch.setattr(jobs, 'collect_targets', lambda *a, **kw: (0, 0, 0))
    enqueue = Mock()
    monkeypatch.setattr(jobs, 'enqueue_daily_reports', enqueue)
    monkeypatch.setattr(jobs, '_deliver', lambda factory: (0, 0))
    jobs.execute(db, 'watch-slots')
    enqueue.assert_not_called()


def test_report_plan_is_due_user_scoped_and_bypasses_poll_cooldown(db):
    with db.begin() as s:
        s.add(CollectionRun(site_id=1, facility_id=1, started_at=NOW-timedelta(minutes=1), status='SUCCESS'))
    with db() as s:
        assert build_collection_plan(s, now=NOW) == []
        assert len(build_collection_plan(s, now=NOW, report_only=True)) == 1
    with db.begin() as s:
        s.get(DailyReportSetting, 1).enabled = False
    with db() as s:
        assert build_collection_plan(s, now=NOW, report_only=True) == []


def test_lock_conflict_is_failure_not_false_success(db, monkeypatch):
    @contextmanager
    def busy(*args):
        yield False
    receipt(db)
    monkeypatch.setattr(jobs, 'advisory_job_lock', busy)
    assert jobs.run(db, 'watch:one', 'watch-slots', '1') == 1
    with db() as s:
        assert s.get(SchedulerJob, 'watch:one').error_code == 'JOB_LOCK_BUSY'


def test_missing_receipt_is_not_success(db):
    assert jobs.run(db, 'missing', 'watch-slots', '1') == 1


def test_freshness_detects_missing_success_with_old_observation(db):
    from test_daily_reports import slot, PLAY
    from tenniswatch.collection import CollectionTarget
    from tenniswatch.collectors.base import FacilityRef
    from datetime import datetime, timezone
    target = CollectionTarget(1, 'fake', FacilityRef(1, 'test', 'f1', 'https://example.test'), (PLAY,))
    assert jobs.check_freshness(db, [target])['unverified_dates'] == 1
    slot(db, observed=datetime.now(timezone.utc)-timedelta(hours=5))
    with db.begin() as s:
        s.add(CollectionRun(site_id=1, facility_id=1, requested_dates=[PLAY.isoformat()],
            started_at=NOW, status='SUCCESS'))
    assert jobs.check_freshness(db, [target])['stale_dates'] == 1


def test_advisory_lock_pins_connection_until_unlock():
    from tenniswatch.database import advisory_job_lock
    session = Mock()
    connection = Mock()
    context = session.get_bind.return_value.connect.return_value
    context.__enter__ = Mock(return_value=connection)
    context.__exit__ = Mock(return_value=False)
    connection.execute.return_value.scalar.return_value = True
    with advisory_job_lock(session, 42) as acquired:
        assert acquired
        context.__exit__.assert_not_called()
    assert connection.execute.call_count == 2
    session.commit.assert_not_called()
