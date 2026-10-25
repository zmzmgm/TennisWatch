"""Offline collection planning and atomic persistence tests; no live reservation data."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tenniswatch.cli import seed_gangseo
from tenniswatch.collection import build_collection_plan, collect_targets
from tenniswatch.collectors.base import BaseCollector, CollectorRegistry
from tenniswatch.database import Base
from tenniswatch.domain import SlotObservation
from tenniswatch.models import (AvailabilitySlot, CollectionRun, Court, Facility, Notification,
    ReservationSite, Subscription, TelegramUser, UserWatch, UserWatchFacility)
from tenniswatch.services import apply_observation

NOW = datetime(2026, 10, 24, 3, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    seed_gangseo(factory)
    yield factory
    engine.dispose()


def add_watch(factory, day, **overrides):
    with factory.begin() as session:
        facility = session.scalar(select(Facility))
        user = TelegramUser(chat_id="123", telegram_user_id=123)
        session.add(user)
        session.flush()
        values = dict(id=10, user_id=user.id, watch_type="availability",
                      criteria={"play_date": day.isoformat()}, criteria_hash="test", enabled=True)
        values.update(overrides)
        watch = UserWatch(**values)
        session.add(watch)
        session.flush()
        session.add(UserWatchFacility(user_watch_id=watch.id, facility_id=facility.id))
        return facility.id, user.id


class FakeCollector(BaseCollector):
    site_key = "gangseo_seoul"

    def __init__(self, *, error=None, mismatch=False):
        self.error = error
        self.mismatch = mismatch
        self.calls = []
        self.closed = False

    def collect_slots(self, facility, day):
        self.calls.append((facility.id, day))
        if self.error:
            raise self.error
        obs = SlotObservation(f"{day}:1코트:1800-2000", facility.id, day, time(18), time(20),
                              "1코트", True, facility.booking_url, datetime.now(timezone.utc), "2")
        return [obs, replace(obs, facility_id=999)] if self.mismatch else [obs]

    def close(self):
        self.closed = True


def registry(collector):
    result = CollectorRegistry()
    result.register(collector)
    return result


def test_seed_is_idempotent_and_includes_the_verified_court(db):
    seed_gangseo(db)
    with db() as session:
        assert len(list(session.scalars(select(Facility)))) == 1
        assert [(row.external_id, row.name) for row in session.scalars(select(Court))] == [("2", "1코트")]


def test_plan_deduplicates_legacy_and_new_watches_in_seoul_local_date(db):
    now = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)  # Seoul is already Saturday.
    facility_id, user_id = add_watch(db, date(2026, 10, 24))
    with db.begin() as session:
        session.add(Subscription(user_id=user_id, facility_id=facility_id, weekdays="6", enabled=True))
    with db() as session:
        targets = build_collection_plan(session, 2, now=now)
    assert len(targets) == 1
    assert targets[0].play_dates == (date(2026, 10, 24),)


def test_site_poll_interval_and_expired_or_unsupported_watches_are_skipped(db):
    now = NOW
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    add_watch(db, today)
    with db.begin() as session:
        site = session.scalar(select(ReservationSite))
        site.poll_minutes = 30
        session.add(CollectionRun(site_id=site.id, status="ERROR", started_at=now - timedelta(minutes=10)))
    with db() as session:
        assert build_collection_plan(session, now=now) == []
        assert len(build_collection_plan(session, now=now + timedelta(minutes=21))) == 1
    with db.begin() as session:
        session.get(UserWatch, 10).expires_at = now - timedelta(seconds=1)
    with db() as session:
        assert build_collection_plan(session, now=now + timedelta(minutes=21)) == []
    with db.begin() as session:
        session.get(UserWatch, 10).expires_at = None
        session.scalar(select(Facility)).collection_status = "UNVERIFIED"
    with db() as session:
        assert build_collection_plan(session, now=now + timedelta(minutes=21)) == []


def test_no_subscriptions_make_no_collector_requests(db):
    collector = FakeCollector()
    assert collect_targets(db, registry(collector)) == (0, 0, 0)
    assert collector.calls == []


def test_explicit_30_minute_override_supports_half_hourly_monitoring(db):
    add_watch(db, NOW.astimezone(ZoneInfo("Asia/Seoul")).date())
    with db.begin() as session:
        site = session.scalar(select(ReservationSite))
        session.add(CollectionRun(site_id=site.id, started_at=NOW - timedelta(minutes=31), status="SUCCESS"))
    with db() as session:
        assert build_collection_plan(session, now=NOW) == []
        assert len(build_collection_plan(session, now=NOW, poll_interval_minutes=30)) == 1


def test_success_saves_normalized_slot_and_failure_preserves_it_without_new_alerts(db):
    now = NOW
    day = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    add_watch(db, day)
    collector = FakeCollector()
    assert collect_targets(db, registry(collector), now=now) == (1, 1, 0)
    assert collector.closed
    with db() as session:
        slot = session.scalar(select(AvailabilitySlot))
        saved_at = slot.observed_at
        assert slot.status == "AVAILABLE" and slot.court_id is not None
        assert list(session.scalars(select(Notification))) == []  # new watch alerts are Phase 6
    failing = FakeCollector(error=RuntimeError("private reservation-person detail"))
    assert collect_targets(db, registry(failing), now=now + timedelta(minutes=61)) == (1, 0, 1)
    with db() as session:
        slot = session.scalar(select(AvailabilitySlot))
        assert slot.status == "AVAILABLE" and slot.observed_at == saved_at
        run = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()))
        assert run.status == "ERROR" and run.error_code == "COLLECTION_FAILED"
        assert "private" not in run.error and run.duration_ms is not None


def test_malformed_snapshot_never_partially_writes_slots(db):
    day = NOW.astimezone(ZoneInfo("Asia/Seoul")).date()
    add_watch(db, day)
    assert collect_targets(db, registry(FakeCollector(mismatch=True)), now=NOW) == (1, 0, 1)
    with db() as session:
        assert list(session.scalars(select(AvailabilitySlot))) == []
        assert session.scalar(select(CollectionRun)).error_code == "INVALID_SNAPSHOT"


def test_older_or_replayed_observation_does_not_overwrite_newer_state(db):
    with db.begin() as session:
        facility = session.scalar(select(Facility))
        newer = SlotObservation("stable-slot", facility.id, date(2026, 10, 24), time(18), time(20),
            "1코트", True, facility.booking_url, datetime(2026, 10, 24, tzinfo=timezone.utc), "2")
        apply_observation(session, newer)
        apply_observation(session, replace(newer, available=False, observed_at=newer.observed_at - timedelta(minutes=1)))
        apply_observation(session, replace(newer, available=False))
        assert session.scalar(select(AvailabilitySlot)).status == "AVAILABLE"
