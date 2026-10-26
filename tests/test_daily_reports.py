from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tenniswatch.daily_reports import configure_daily_report, enqueue_daily_reports, next_daily_run, render_daily_report
from tenniswatch.database import Base
from tenniswatch.models import (AvailabilitySlot, CollectionRun, DailyReportSetting, Facility, Notification,
    ReservationSite, Subscription, TelegramUser, UserWatch, UserWatchFacility)
from tenniswatch.services import dispatch_due_notifications
from tenniswatch.notifications import NotificationDeliveryError

NOW = datetime(2026,10,25,0,5,tzinfo=timezone.utc)  # 09:05 KST
PLAY = date(2026,10,26)


@pytest.fixture
def db():
    engine=create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory.begin() as s:
        s.add(ReservationSite(id=1,key='fake',name='모의 사이트',base_url='https://example.test',poll_minutes=60))
        s.add(Facility(id=1,site_id=1,external_id='f1',name='모의 코트',collection_status='SUPPORTED',booking_url='https://example.test/book'))
        s.add_all([TelegramUser(id=1,chat_id='owner',telegram_user_id=111), TelegramUser(id=2,chat_id='other',telegram_user_id=222)])
        s.add(UserWatch(id=1,user_id=1,watch_type='availability',criteria={'play_date':str(PLAY),'start_time':'18:00','end_time':'22:00'},criteria_hash='one',monitoring_status='ACTIVE'))
        s.add(UserWatchFacility(user_watch_id=1,facility_id=1))
        configure_daily_report(s,1,9*60,now=NOW-timedelta(hours=1))
    yield factory
    engine.dispose()


def slot(factory, *, status='AVAILABLE', observed=NOW, start=time(18), key='slot'):
    with factory.begin() as s:
        s.add(AvailabilitySlot(facility_id=1,slot_key=key,court_name='1코트',play_date=PLAY,
            starts_at=start,ends_at=time(22),status=status,booking_url='https://example.test/book',observed_at=observed))


class Sender:
    def __init__(self,fail=False): self.calls=[]; self.fail=fail
    def send(self,text,chat_id=None):
        self.calls.append((text,chat_id))
        if self.fail: raise NotificationDeliveryError('TELEGRAM_503')


def test_korea_next_run_and_midnight():
    assert next_daily_run(9*60,NOW)==datetime(2026,10,26,0,0,tzinfo=timezone.utc)
    assert next_daily_run(0,NOW)==datetime(2026,10,25,15,0,tzinfo=timezone.utc)
    assert next_daily_run(9*60+6,NOW)==NOW+timedelta(minutes=1)
    with pytest.raises(ValueError): next_daily_run(1440,NOW)


def test_due_report_is_once_per_user_per_day_even_after_rewind(db):
    slot(db)
    assert enqueue_daily_reports(db,now=NOW-timedelta(minutes=6))==0
    assert enqueue_daily_reports(db,now=NOW)==1
    assert enqueue_daily_reports(db,now=NOW)==0
    with db.begin() as s: s.get(DailyReportSetting,1).next_run_at=NOW-timedelta(hours=1)
    assert enqueue_daily_reports(db,now=NOW)==0
    sender=Sender()
    assert dispatch_due_notifications(db,sender,now=NOW)==(1,0)
    assert sender.calls[0][1]=='owner'
    assert '예약 가능' in sender.calls[0][0] and '마지막 확인' in sender.calls[0][0]
    assert 'https://example.test/book' in sender.calls[0][0]
    assert dispatch_due_notifications(db,sender,now=NOW)==(0,0)


def test_no_availability_sends_explicit_none(db):
    slot(db,status='UNAVAILABLE')
    enqueue_daily_reports(db,now=NOW)
    sender=Sender(); dispatch_due_notifications(db,sender,now=NOW)
    assert '예약 가능한 코트가 없습니다' in sender.calls[0][0]


@pytest.mark.parametrize('mode',['stale','missing','failed','running','unknown'])
def test_unverified_data_never_means_sold_out(db,mode):
    if mode!='missing': slot(db,observed=NOW-timedelta(hours=3) if mode=='stale' else NOW,status='UNKNOWN' if mode=='unknown' else 'AVAILABLE')
    if mode in ('failed','running'):
        with db.begin() as s:
            s.add(CollectionRun(site_id=1,facility_id=1,requested_dates=[str(PLAY)],status='ERROR' if mode=='failed' else 'RUNNING',started_at=NOW+timedelta(seconds=1)))
    with db() as s: text=render_daily_report(s,1,NOW)
    assert '확정할 수 없습니다' in text
    assert '예약 가능한 코트가 없습니다' not in text
    assert '1코트 · 예약 가능' not in text


def test_current_conditions_filter_other_users_dates_and_hours(db):
    slot(db,start=time(17))
    with db.begin() as s:
        s.add(UserWatch(id=2,user_id=2,watch_type='availability',criteria={'play_date':str(PLAY),'start_time':'17:00'},criteria_hash='other',monitoring_status='ACTIVE'))
        s.add(UserWatchFacility(user_watch_id=2,facility_id=1))
    with db() as s:
        text=render_daily_report(s,1,NOW)
        assert '1코트 · 예약 가능' not in text
        assert '1코트 · 예약 가능' in render_daily_report(s,2,NOW)


def test_legacy_filters_and_duplicate_slot_deduplication(db):
    slot(db)
    with db.begin() as s:
        s.add(Subscription(user_id=1,facility_id=1,weekdays='1',start_time=time(18),end_time=time(22)))
    with db() as s: text=render_daily_report(s,1,NOW)
    assert text.count('1코트 · 예약 가능')==1


def test_retry_rebuilds_current_availability_and_retains_daily_event(db):
    slot(db); enqueue_daily_reports(db,now=NOW)
    assert dispatch_due_notifications(db,Sender(fail=True),now=NOW)==(0,1)
    with db.begin() as s:
        row=s.scalar(select(AvailabilitySlot)); row.status='UNAVAILABLE'; row.observed_at=NOW+timedelta(minutes=3)
    sender=Sender()
    assert dispatch_due_notifications(db,sender,now=NOW+timedelta(minutes=3))==(1,0)
    assert '예약 가능한 코트가 없습니다' in sender.calls[0][0]
    with db() as s: assert len(list(s.scalars(select(Notification))))==1


@pytest.mark.parametrize('change',['disable','time','expired','user_disabled'])
def test_delivery_rechecks_opt_in_time_version_day_and_user(db,change):
    slot(db); enqueue_daily_reports(db,now=NOW)
    with db.begin() as s:
        if change=='disable': configure_daily_report(s,1,9*60,enabled=False,now=NOW)
        elif change=='time': configure_daily_report(s,1,18*60,now=NOW)
        elif change=='user_disabled': s.get(TelegramUser,1).enabled=False
    sender=Sender()
    assert dispatch_due_notifications(db,sender,now=NOW+timedelta(days=1) if change=='expired' else NOW)==(0,0)
    assert not sender.calls


def test_missed_days_are_not_backfilled(db):
    with db.begin() as s: s.get(DailyReportSetting,1).next_run_at=NOW-timedelta(days=3)
    early=NOW-timedelta(minutes=10)
    assert enqueue_daily_reports(db,now=early)==0
    assert enqueue_daily_reports(db,now=NOW)==1
    with db() as s: assert s.scalar(select(Notification)).report_date==NOW.date()


def test_configuration_is_idempotent_and_after_time_starts_tomorrow(db):
    with db.begin() as s:
        setting=s.get(DailyReportSetting,1); revision=setting.revision; due=setting.next_run_at
        setting=configure_daily_report(s,1,9*60,now=NOW)
        assert setting.revision==revision and setting.next_run_at==due
        setting=configure_daily_report(s,1,8*60,now=NOW)
        assert setting.revision==revision+1 and setting.next_run_at.replace(tzinfo=timezone.utc)==datetime(2026,10,25,23,0,tzinfo=timezone.utc)


def test_empty_conditions_are_reported_only_after_user_opt_in(db):
    with db.begin() as s: s.get(UserWatch,1).enabled=False
    assert enqueue_daily_reports(db,now=NOW)==1
    sender=Sender(); dispatch_due_notifications(db,sender,now=NOW)
    assert '감시 조건이 없습니다' in sender.calls[0][0]
    with db() as s: assert s.get(DailyReportSetting,2) is None


def test_cli_saves_for_only_the_linked_user_and_requires_valid_time(db,monkeypatch,capsys):
    import sys
    from tenniswatch import cli
    monkeypatch.setattr(cli,'load_dotenv',lambda:None)
    monkeypatch.setattr(cli,'make_session_factory',lambda:db)
    monkeypatch.setenv('DATABASE_URL','unused')
    monkeypatch.setenv('TELEGRAM_CHAT_ID','owner')
    monkeypatch.setenv('WATCH_INTERVAL_MINUTES','')
    monkeypatch.setenv('WATCH_DAYS_AHEAD','7')
    monkeypatch.setattr(sys,'argv',['tenniswatch','set-daily-report','--report-time','18:30'])
    cli.main()
    assert 'owner' not in capsys.readouterr().out
    with db() as s:
        assert s.get(DailyReportSetting,1).minute_of_day==18*60+30
        assert s.get(DailyReportSetting,2) is None
    monkeypatch.setattr(sys,'argv',['tenniswatch','set-daily-report','--report-time','25:00'])
    with pytest.raises(SystemExit): cli.main()


def test_legacy_overnight_window_keeps_existing_filter_semantics(db):
    slot(db,start=time(21))
    with db.begin() as s:
        s.get(UserWatch,1).enabled=False
        s.add(Subscription(user_id=1,facility_id=1,weekdays='1',start_time=time(20),end_time=time(2)))
    with db() as s:
        assert '1코트 · 예약 가능' in render_daily_report(s,1,NOW)


def test_daily_queue_count_uses_insert_result_when_driver_rowcount_is_unknown(db,monkeypatch):
    from sqlalchemy.orm import Session
    original=Session.execute
    class UnknownRowCount:
        rowcount=-1
        def __init__(self,result): self.result=result
        def scalar_one_or_none(self): return self.result.scalar_one_or_none()
    def execute(session,statement,*args,**kwargs):
        result=original(session,statement,*args,**kwargs)
        if getattr(statement,'is_insert',False) and statement.table.name=='notifications':
            return UnknownRowCount(result)
        return result
    monkeypatch.setattr(Session,'execute',execute)
    assert enqueue_daily_reports(db,now=NOW)==1
    with db.begin() as s: s.get(DailyReportSetting,1).next_run_at=NOW-timedelta(minutes=1)
    assert enqueue_daily_reports(db,now=NOW)==0
