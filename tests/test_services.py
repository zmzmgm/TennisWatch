from datetime import datetime, timezone
from types import SimpleNamespace
from tenniswatch.services import record_delivery_failure


def test_failed_delivery_sets_retry_with_exponential_backoff():
    notification = SimpleNamespace(attempts=3, status="SENDING", last_error=None, next_attempt_at=None)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record_delivery_failure(notification, "temporary outage", now)
    assert notification.status == "RETRY"
    assert notification.last_error == "temporary outage"
    assert notification.next_attempt_at == datetime(2026, 10, 1, 0, 8, tzinfo=timezone.utc)

from datetime import date, time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from tenniswatch.database import Base
from tenniswatch.domain import SlotObservation
from tenniswatch.models import Facility, ReservationSite, Subscription, TelegramUser, Notification, AvailabilityEvent
from tenniswatch.services import process_observation


def make_db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def seed_subscriber(session):
    site = ReservationSite(key="test", name="test", base_url="https://example.test")
    session.add(site)
    session.flush()
    facility = Facility(site_id=site.id, external_id="f1", name="센터", booking_url="https://example.test/book", collection_status="SUPPORTED")
    user = TelegramUser(chat_id="chat-1")
    session.add_all([facility, user])
    session.flush()
    session.add(Subscription(user_id=user.id, facility_id=facility.id, weekdays="6", start_time=time(17), end_time=time(21)))
    session.flush()
    return facility.id, user.id


def observation(facility_id, available=True, day=date(2026, 10, 24), start=time(18)):
    key = f"{day.isoformat()}-court-2-{start.strftime('%H%M')}"
    return SlotObservation(key, facility_id, day, start, time(20), "2번 코트", available,
                           "https://example.test/book", datetime(2026, 10, 1, tzinfo=timezone.utc))


def test_first_available_transition_queues_only_matching_subscription():
    Session = make_db()
    with Session.begin() as session:
        facility_id, _ = seed_subscriber(session)
        result = process_observation(session, observation(facility_id))
        assert result.changed and result.notification_created
        queued = list(session.scalars(select(Notification)))
        assert len(queued) == 1
        assert queued[0].status == "PENDING"


def test_repeat_available_does_not_create_duplicate_notification():
    Session = make_db()
    with Session.begin() as session:
        facility_id, _ = seed_subscriber(session)
        process_observation(session, observation(facility_id))
    with Session.begin() as session:
        result = process_observation(session, observation(facility_id))
        assert not result.changed
        assert len(list(session.scalars(select(Notification)))) == 1


def test_unavailable_then_available_creates_new_event_notification():
    from dataclasses import replace
    from datetime import timedelta
    Session = make_db()
    with Session.begin() as session:
        facility_id, _ = seed_subscriber(session)
        process_observation(session, observation(facility_id, False))
        later = replace(observation(facility_id, True), observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(seconds=1))
        process_observation(session, later)
        assert len(list(session.scalars(select(AvailabilityEvent)))) == 2
        assert len(list(session.scalars(select(Notification)))) == 1


def test_weekday_and_time_filters_are_applied():
    Session = make_db()
    with Session.begin() as session:
        facility_id, _ = seed_subscriber(session)
        result = process_observation(session, observation(facility_id, day=date(2026, 10, 23)))
        assert result.notification_created
        assert len(list(session.scalars(select(Notification)))) == 0
        result = process_observation(session, observation(facility_id, day=date(2026, 10, 24), start=time(12)))
        assert result.notification_created
        assert len(list(session.scalars(select(Notification)))) == 0


def test_timezone_aware_slot_matches_legacy_local_time_preferences():
    from dataclasses import replace
    from datetime import timedelta
    from tenniswatch.services import _slot_matches_subscription
    aware = replace(observation(1), starts_at=time(18, tzinfo=timezone(timedelta(hours=9))),
                    ends_at=time(20, tzinfo=timezone(timedelta(hours=9))))
    assert _slot_matches_subscription(aware, "6", time(17), time(21))
    assert not _slot_matches_subscription(aware, "6", time(19), time(21))

from tenniswatch.services import dispatch_due_notifications


class FakeSender:
    def __init__(self, fails=False):
        self.fails = fails
        self.calls = []

    def send(self, text, chat_id=None):
        self.calls.append((text, chat_id))
        if self.fails:
            raise RuntimeError("temporary Telegram error")


def create_pending_notification(Session):
    with Session.begin() as session:
        facility_id, _ = seed_subscriber(session)
        process_observation(session, observation(facility_id))
        session.scalar(select(Notification)).next_attempt_at = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_outbox_dispatch_marks_success_and_uses_subscriber_chat_id():
    Session = make_db()
    create_pending_notification(Session)
    sender = FakeSender()
    sent, failed = dispatch_due_notifications(Session, sender, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert (sent, failed) == (1, 0)
    assert sender.calls[0][1] == "chat-1"
    assert "예약 가능 여부는 변동될 수 있습니다" in sender.calls[0][0]
    with Session() as session:
        row = session.scalar(select(Notification))
        assert row.status == "SENT"
        assert row.sent_at is not None


def test_outbox_dispatch_failure_becomes_retryable():
    Session = make_db()
    create_pending_notification(Session)
    sent, failed = dispatch_due_notifications(Session, FakeSender(fails=True), now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert (sent, failed) == (0, 1)
    with Session() as session:
        row = session.scalar(select(Notification))
        assert row.status == "RETRY"
        assert row.attempts == 1
        assert row.last_error == "DELIVERY_FAILED"
