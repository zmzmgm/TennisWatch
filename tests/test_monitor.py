"""Offline monitor/outbox integration tests. No Telegram, LLM or live site calls."""
from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tenniswatch.collection import collect_targets
from tenniswatch.collectors.base import BaseCollector, CollectorRegistry
from tenniswatch.database import Base
from tenniswatch.domain import SlotObservation, as_utc
from tenniswatch.models import (AvailabilityEvent, AvailabilitySlot, CollectionRun, Facility, Notification,
    PolicyChange, ReservationPolicy, ReservationSchedule, ReservationSite, Subscription, TelegramUser,
    UserWatch, UserWatchFacility)
from tenniswatch.notifications import NotificationDeliveryError, TelegramConfig, TelegramSender
from tenniswatch.opening import check_opening_schedules, track_policy_change
from tenniswatch.services import claim_due_notifications, dispatch_due_notifications, process_observation

NOW = datetime(2026, 10, 23, 0, tzinfo=timezone.utc)
PLAY = date(2026, 10, 24)
OPEN_PLAY = date(2026, 10, 25)
OPENS = datetime(2026, 10, 23, 1, tzinfo=timezone.utc)  # 10:00 Seoul


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(ReservationSite(id=1, key="fake", name="모의 사이트", base_url="https://example.test", poll_minutes=60))
        session.add(Facility(id=1, site_id=1, external_id="fake", name="모의 테니스장", collection_status="SUPPORTED", booking_url="https://example.test/book"))
        session.add(TelegramUser(id=1, chat_id="private-chat", telegram_user_id=123))
    yield factory
    engine.dispose()


def add_watch(factory, *, watch_id=100, user_id=1, kind="availability", status="ACTIVE", criteria=None, **extra):
    with factory.begin() as session:
        session.add(UserWatch(id=watch_id, user_id=user_id, watch_type=kind,
            criteria=criteria or {"play_date": PLAY.isoformat(), "start_time": "18:00", "end_time": "22:00"},
            criteria_hash=str(watch_id), monitoring_status=status, **extra))
        session.add(UserWatchFacility(user_watch_id=watch_id, facility_id=1))


def observe(factory, available=True, at=NOW):
    with factory.begin() as session:
        return process_observation(session, SlotObservation("same-slot", 1, PLAY, time(18), time(20),
            "1코트", available, "https://example.test/book", at), now=at)


class Sender:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def send(self, text, chat_id=None):
        self.calls.append((text, chat_id))
        if self.error:
            raise self.error


def test_first_success_activates_pending_watch_and_queues_initial_availability(db):
    add_watch(db, status="PENDING_ACTIVATION")
    class Adapter(BaseCollector):
        site_key = "fake"
        def collect_slots(self, facility, day):
            return [SlotObservation("same-slot", facility.id, day, time(18), time(20), "1코트",
                True, facility.booking_url, NOW)]
    registry = CollectorRegistry()
    registry.register(Adapter())
    assert collect_targets(db, registry, 1, now=NOW, activate_watches=True) == (1, 1, 0)
    with db() as session:
        assert session.get(UserWatch, 100).monitoring_status == "ACTIVE"
        assert len(list(session.scalars(select(Notification)))) == 1
    sender = Sender()
    assert dispatch_due_notifications(db, sender, now=NOW) == (1, 0)
    assert sender.calls[0][1] == "private-chat"


def test_repeated_available_is_silent_but_reopening_sends_a_new_event(db):
    add_watch(db)
    sender = Sender()
    observe(db)
    assert dispatch_due_notifications(db, sender, now=NOW) == (1, 0)
    observe(db, at=NOW + timedelta(minutes=10))
    assert dispatch_due_notifications(db, sender, now=NOW + timedelta(minutes=10)) == (0, 0)
    observe(db, False, NOW + timedelta(minutes=20))
    observe(db, True, NOW + timedelta(minutes=30))
    assert dispatch_due_notifications(db, sender, now=NOW + timedelta(minutes=30)) == (1, 0)
    assert len(sender.calls) == 2


def test_collection_failure_does_not_activate_pending_watch(db):
    add_watch(db, status="PENDING_ACTIVATION")
    class FailingAdapter(BaseCollector):
        site_key = "fake"
        def collect_slots(self, facility, day):
            raise RuntimeError("site unavailable")
    registry = CollectorRegistry()
    registry.register(FailingAdapter())
    assert collect_targets(db, registry, 1, now=NOW, activate_watches=True) == (1, 0, 1)
    with db() as session:
        assert session.get(UserWatch, 100).monitoring_status == "PENDING_ACTIVATION"
        assert list(session.scalars(select(Notification))) == []


def test_overlapping_legacy_and_new_watches_deduplicate_per_event_and_user(db):
    add_watch(db)
    add_watch(db, watch_id=101, criteria={"weekdays": [6], "start_time": "17:00"})
    with db.begin() as session:
        session.add(Subscription(user_id=1, facility_id=1, enabled=True))
    observe(db)
    observe(db, at=NOW + timedelta(minutes=1))
    with db() as session:
        assert len(list(session.scalars(select(Notification)))) == 1


def test_new_subscriber_receives_existing_available_event_without_renotifying_old_user(db):
    add_watch(db)
    observe(db)
    assert dispatch_due_notifications(db, Sender(), now=NOW) == (1, 0)
    with db.begin() as session:
        session.add(TelegramUser(id=2, chat_id="second-chat", telegram_user_id=456))
    add_watch(db, watch_id=101, user_id=2)
    result = observe(db, at=NOW + timedelta(minutes=1))
    assert not result.changed
    sender = Sender()
    assert dispatch_due_notifications(db, sender, now=NOW + timedelta(minutes=1)) == (1, 0)
    assert sender.calls[0][1] == "second-chat"


@pytest.mark.parametrize("criteria,extra", [
    ({"play_date": "2026-10-25"}, {}),
    ({"weekdays": [5]}, {}),
    ({"play_date": "2026-10-24", "start_time": "19:00"}, {}),
    ({"play_date": "2026-10-24"}, {"expires_at": NOW - timedelta(seconds=1)}),
    ({"play_date": "2026-10-24"}, {"enabled": False}),
])
def test_date_time_expiry_and_disable_filters_prevent_alerts(db, criteria, extra):
    add_watch(db, criteria=criteria, **extra)
    observe(db)
    with db() as session:
        assert list(session.scalars(select(Notification))) == []


def test_unsubscribe_or_slot_close_before_delivery_suppresses_queued_alert(db):
    add_watch(db)
    observe(db)
    with db.begin() as session:
        session.get(UserWatch, 100).enabled = False
    sender = Sender()
    assert dispatch_due_notifications(db, sender, now=NOW) == (0, 0)
    with db() as session:
        assert session.scalar(select(Notification)).status == "SKIPPED"
    with db.begin() as session:
        session.get(UserWatch, 100).enabled = True
    observe(db, at=NOW + timedelta(minutes=1))
    observe(db, False, NOW + timedelta(minutes=2))
    assert dispatch_due_notifications(db, sender, now=NOW + timedelta(minutes=2)) == (0, 0)
    assert sender.calls == []


@pytest.mark.parametrize("source_error", [False, True])
def test_stale_or_failed_collection_defers_without_sending_or_consuming_retries(db, source_error):
    add_watch(db)
    observe(db)
    when = NOW + timedelta(minutes=1) if source_error else NOW + timedelta(hours=3)
    if source_error:
        with db.begin() as session:
            session.add(CollectionRun(site_id=1, facility_id=1, requested_dates=[PLAY.isoformat()],
                started_at=NOW + timedelta(seconds=20), status="ERROR", error_code="HTTP_ERROR"))
    sender = Sender()
    assert dispatch_due_notifications(db, sender, now=when) == (0, 0)
    assert sender.calls == []
    with db() as session:
        row = session.scalar(select(Notification))
        assert row.status == "RETRY" and row.attempts == 0
        assert row.last_error == ("COLLECTION_FAILED" if source_error else "STALE_OBSERVATION")


def test_delivery_failure_is_safe_and_retryable_then_succeeds(db):
    add_watch(db)
    observe(db)
    assert dispatch_due_notifications(db, Sender(RuntimeError("secret-token / private-chat")), now=NOW) == (0, 1)
    with db() as session:
        row = session.scalar(select(Notification))
        assert row.status == "RETRY" and row.last_error == "DELIVERY_FAILED"
        retry_at = as_utc(row.next_attempt_at)
    assert dispatch_due_notifications(db, Sender(), now=retry_at) == (1, 0)


def test_permanent_telegram_error_and_exhausted_lease_are_terminal(db):
    add_watch(db)
    observe(db)
    assert dispatch_due_notifications(db, Sender(NotificationDeliveryError("TELEGRAM_403", permanent=True)), now=NOW) == (0, 1)
    with db.begin() as session:
        row = session.scalar(select(Notification))
        assert row.status == "FAILED"
        row.status = "SENDING"
        row.locked_at = NOW - timedelta(minutes=16)
        row.attempts = 8
    with db.begin() as session:
        assert claim_due_notifications(session, now=NOW) == []
        assert session.scalar(select(Notification)).status == "FAILED"


def test_duplicate_claims_are_blocked_and_old_sender_cannot_ack_new_lease(db):
    add_watch(db)
    observe(db)
    with db.begin() as session:
        first = claim_due_notifications(session, now=NOW)[0]
        old_token = first.lease_token
    with db.begin() as session:
        assert claim_due_notifications(session, now=NOW) == []
        first = session.scalar(select(Notification))
        first.locked_at = NOW - timedelta(minutes=16)
    class LeaseStealingSender(Sender):
        def send(self, text, chat_id=None):
            with db.begin() as session:
                row = session.scalar(select(Notification))
                row.locked_at = NOW - timedelta(minutes=16)
                claim_due_notifications(session, now=NOW)
    assert dispatch_due_notifications(db, LeaseStealingSender(), now=NOW) == (0, 0)
    with db() as session:
        row = session.scalar(select(Notification))
        assert row.status == "SENDING" and row.lease_token != old_token


def add_policy(factory, *, verified=True, audience="resident", days=2):
    with factory.begin() as session:
        policy = ReservationPolicy(facility_id=1, audience=audience, open_days_before=days,
            open_time=time(10), timezone_name="Asia/Seoul", verified=verified,
            source_url="https://example.test/policy")
        session.add(policy)
        session.flush()
        return policy.id


def test_verified_opening_reminders_have_disjoint_delay_windows_and_are_idempotent(db):
    add_watch(db, kind="opening", status="PENDING_ACTIVATION", criteria={"play_date": OPEN_PLAY.isoformat(), "audience": "resident"})
    add_policy(db)
    day_before = OPENS - timedelta(hours=23)
    result = check_opening_schedules(db, now=day_before, activate_watches=True)
    assert result["notifications"] == 1 and result["activated"] == 1
    assert check_opening_schedules(db, now=day_before, activate_watches=True)["notifications"] == 0
    sender = Sender()
    assert dispatch_due_notifications(db, sender, now=day_before) == (1, 0)
    assert "지역주민 우선 예약" in sender.calls[0][0] and "2026-10-23 10:00" in sender.calls[0][0]
    hour_before = OPENS - timedelta(minutes=30)
    assert check_opening_schedules(db, now=hour_before, activate_watches=True)["notifications"] == 1
    assert dispatch_due_notifications(db, sender, now=hour_before) == (1, 0)
    opened = OPENS + timedelta(seconds=1)
    assert check_opening_schedules(db, now=opened, activate_watches=True)["notifications"] == 1
    assert check_opening_schedules(db, now=opened, activate_watches=True)["notifications"] == 0
    assert dispatch_due_notifications(db, sender, now=opened) == (1, 0)
    assert "예약 오픈 예정 시각이 지났어요" in sender.calls[-1][0]


def test_weekly_policy_remains_verified_after_reload_and_activates_watch(db):
    checked = datetime(2026, 10, 26, tzinfo=timezone.utc)
    add_watch(db, kind="opening", status="PENDING_ACTIVATION", criteria={"play_date": "2026-10-28"})
    with db.begin() as session:
        policy = ReservationPolicy(facility_id=1, audience="general", open_days_before=0,
            open_time=time(14), timezone_name="Asia/Seoul", verified=False,
            rule_type="weekly_batch", open_weekday=2, window_start_days=1, window_days=7,
            source_url="https://example.test/policy", source_digest="a" * 64, source_checked_at=checked)
        session.add(policy)
        session.flush()
        track_policy_change(session, policy, verify=True, now=checked)
    result = check_opening_schedules(db, now=checked, activate_watches=True)
    assert result["policy_changes"] == 0
    assert result["activated"] == 1
    with db() as session:
        assert session.scalar(select(ReservationPolicy)).verified
        assert session.scalar(select(UserWatch)).monitoring_status == "ACTIVE"
    # 정책 재승인이 일정을 무효화한 뒤에도 다음 점검은 검증된 규칙으로 복구한다.
    with db.begin() as session:
        session.scalar(select(ReservationSchedule)).verified = False
    recovered = check_opening_schedules(db, now=checked, activate_watches=True)
    assert recovered["policy_changes"] == 0
    assert recovered["notifications"] == 0
    with db() as session:
        assert session.scalar(select(ReservationSchedule)).verified


def test_policy_status_diagnostic_exposes_only_rule_values():
    import json
    from tenniswatch.opening_status import policy_change_diagnostic
    diagnostic = policy_change_diagnostic(PolicyChange(policy_id=1,
        old_value=json.dumps({"open_time": "14:00:00", "source_url": "private-before"}),
        new_value=json.dumps({"open_time": "15:00:00", "source_url": "private-after"})))
    assert diagnostic["changed_fields"] == ["open_time", "source_url"]
    assert diagnostic["rule_changes"] == {"open_time": {"before": "14:00:00", "after": "15:00:00"}}
    assert "private" not in json.dumps(diagnostic)


def test_first_late_run_sends_only_hour_reminder_and_respects_audience(db):
    add_watch(db, kind="opening", criteria={"play_date": OPEN_PLAY.isoformat(), "audience": "resident"})
    add_policy(db)
    add_policy(db, audience="general", days=2)
    assert check_opening_schedules(db, now=OPENS - timedelta(minutes=20), activate_watches=True)["notifications"] == 1
    with db() as session:
        assert [row.kind for row in session.scalars(select(Notification))] == ["opening_hour_before"]


@pytest.mark.parametrize("blocker", [None, "SCHEDULE_UNVERIFIED", "POLICY_SOURCE_NOT_CURRENT", "POLICY_SNAPSHOT_CHANGED"])
def test_opening_status_explains_readiness_independently_of_active_watch(db, blocker):
    from tenniswatch.opening_status import opening_status
    add_watch(db, kind="opening", criteria={"play_date": OPEN_PLAY.isoformat()})
    policy_id = add_policy(db)
    check_opening_schedules(db, now=NOW, activate_watches=True)
    with db.begin() as session:
        policy = session.get(ReservationPolicy, policy_id)
        if blocker == "SCHEDULE_UNVERIFIED":
            session.scalar(select(ReservationSchedule)).verified = False
        if blocker == "POLICY_SOURCE_NOT_CURRENT":
            policy.source_digest = "a" * 64
            policy.source_checked_at = NOW - timedelta(hours=37)
            track_policy_change(session, policy)
            policy.verified = True
            session.flush()
            session.scalar(select(ReservationSchedule)).verified = True
        if blocker == "POLICY_SNAPSHOT_CHANGED":
            policy.open_time = time(11)
    status = opening_status(db, "private-chat", now=NOW)
    assert status["pending_activation"] == 0
    row = status["upcoming"][0]
    assert row["policy_ready"] is (blocker is None)
    assert row["readiness_issues"] == ([] if blocker is None else [blocker])


def test_unverified_policy_never_activates_or_enqueues_opening_alert(db):
    add_watch(db, kind="opening", status="PENDING_ACTIVATION", criteria={"play_date": OPEN_PLAY.isoformat()})
    add_policy(db, verified=False)
    result = check_opening_schedules(db, now=NOW, activate_watches=True)
    assert result["activated"] == result["notifications"] == 0
    with db() as session:
        assert not session.scalar(select(ReservationSchedule)).verified


def test_ambiguous_same_audience_policies_are_not_announced(db):
    add_watch(db, kind="opening", criteria={"play_date": OPEN_PLAY.isoformat()})
    add_policy(db)
    add_policy(db, days=1)
    result = check_opening_schedules(db, now=NOW, activate_watches=True)
    assert result["notifications"] == 0
    with db() as session:
        assert all(not policy.verified for policy in session.scalars(select(ReservationPolicy)))


def test_policy_change_is_recorded_and_cancels_old_reminder_until_explicit_review(db):
    add_watch(db, kind="opening", criteria={"play_date": OPEN_PLAY.isoformat()})
    policy_id = add_policy(db)
    when = OPENS - timedelta(hours=23)
    check_opening_schedules(db, now=when, activate_watches=True)
    with db.begin() as session:
        session.get(ReservationPolicy, policy_id).open_time = time(11)
    result = check_opening_schedules(db, now=when, activate_watches=True)
    assert result["policy_changes"] == 1 and result["notifications"] == 0
    assert dispatch_due_notifications(db, Sender(), now=when) == (0, 0)
    with db.begin() as session:
        policy = session.get(ReservationPolicy, policy_id)
        assert not policy.verified
        assert session.get(UserWatch, 100).monitoring_status == "PENDING_ACTIVATION"
        assert len(list(session.scalars(select(PolicyChange)))) == 1
        track_policy_change(session, policy, verify=True, now=when)
    assert check_opening_schedules(db, now=when + timedelta(hours=1), activate_watches=True)["notifications"] == 1


def test_telegram_errors_do_not_expose_token_or_response_body_and_honor_retry_after():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429,
        json={"ok": False, "error_code": 429, "description": "private-data", "parameters": {"retry_after": 123}})))
    sender = TelegramSender(TelegramConfig("super-secret-token"), client)
    with pytest.raises(NotificationDeliveryError) as error:
        sender.send("sample", chat_id="private-chat")
    assert str(error.value) == "TELEGRAM_429" and error.value.retry_after == 123
    assert "secret" not in str(error.value) and "private" not in str(error.value)


def test_direct_policy_review_invalidates_previous_schedule_before_recalculation(db):
    add_watch(db, kind="opening", criteria={"play_date": OPEN_PLAY.isoformat()})
    policy_id = add_policy(db)
    when = OPENS - timedelta(hours=23)
    check_opening_schedules(db, now=when, activate_watches=True)
    with db.begin() as session:
        policy = session.get(ReservationPolicy, policy_id)
        policy.open_time = time(11)
        track_policy_change(session, policy, verify=True, now=when)
        assert not session.scalar(select(ReservationSchedule)).verified
    assert dispatch_due_notifications(db, Sender(), now=when) == (0, 0)


def test_collection_plan_outputs_browser_requirement_without_starting_it(db, monkeypatch, tmp_path):
    import sys
    from tenniswatch import cli
    from tenniswatch.collection import build_collection_plan
    from tenniswatch.collectors.base import CollectorCapabilities
    add_watch(db, status="PENDING_ACTIVATION")
    class BrowserAdapter(BaseCollector):
        site_key = "fake"
        capabilities = CollectorCapabilities(requires_browser=True)
        def collect_slots(self, facility, day):
            raise AssertionError("계획 확인은 브라우저나 수집기를 실행하면 안 됩니다.")
    registry = CollectorRegistry()
    registry.register(BrowserAdapter())
    output = tmp_path / "github-output"
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "make_session_factory", lambda: db)
    monkeypatch.setattr(cli, "default_registry", lambda: registry)
    monkeypatch.setattr(cli, "build_collection_plan", lambda session, days, **kw: build_collection_plan(session, days, now=NOW, **kw))
    monkeypatch.setenv("DATABASE_URL", "unused-in-test")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("WATCH_INTERVAL_MINUTES", "")
    monkeypatch.setenv("WATCH_DAYS_AHEAD", "7")
    monkeypatch.setattr(sys, "argv", ["tenniswatch", "collection-plan", "--github-output"])
    cli.main()
    assert output.read_text().splitlines() == ["has_targets=true", "needs_browser=true"]
