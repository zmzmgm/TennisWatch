"""Verified DB policy scheduling. This module does not claim to scrape site policies."""
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .domain import as_utc, criteria_includes_date
from .models import Facility, Notification, PolicyChange, ReservationPolicy, ReservationSchedule
from .services import calculate_schedules
from .watches import eligible_watches
from .policy_rules import rule_fields, rule_is_valid

OPENED_GRACE = timedelta(hours=3)
DEFAULT_REMINDERS = ("day_before", "hour_before")


def policy_snapshot(policy: ReservationPolicy) -> str:
    return json.dumps({"audience": policy.audience, "open_days_before": policy.open_days_before,
        "open_time": policy.open_time.replace(tzinfo=None).isoformat(), "timezone_name": policy.timezone_name,
        "source_url": policy.source_url, **rule_fields(policy)}, sort_keys=True, ensure_ascii=False)


def policy_snapshot_matches(policy, saved=None):
    try:
        previous = json.loads(saved if saved is not None else policy.last_checked_value)
        if isinstance(previous.get("open_time"), str):
            previous["open_time"] = previous["open_time"][:8]
        # 0009 이전의 일 단위 스냅샷만 호환한다. 새 주간 필드를 생략하면 일치하지 않는다.
        for key, default in {"rule_type": "rolling_days", "open_weekday": None,
                             "window_start_days": None, "window_days": None, "source_digest": None}.items():
            previous.setdefault(key, default)
        return previous == json.loads(policy_snapshot(policy))
    except (TypeError, ValueError, AttributeError):
        return False


def policy_source_current(policy, now):
    if policy.source_check_error:
        return False
    if not policy.source_digest:
        return True  # 기존 수동 검증 정책은 기존 검증 상태를 사용한다.
    return bool(policy.source_checked_at and not policy.source_check_error
                and timedelta(minutes=-1) <= as_utc(now) - as_utc(policy.source_checked_at) <= timedelta(hours=36))


def policy_can_notify(policy, now):
    return bool(policy.verified and policy_is_valid(policy) and policy_source_current(policy, now))


def policy_is_valid(policy: ReservationPolicy) -> bool:
    try:
        ZoneInfo(policy.timezone_name)
        source = urlsplit(policy.source_url)
        return (policy.audience in ("resident", "general") and rule_is_valid(policy)
                and source.scheme == "https" and bool(source.hostname) and not source.username and not source.password)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return False


def track_policy_change(session, policy: ReservationPolicy, *, verify: bool = False, now=None) -> bool:
    current = policy_snapshot(policy)
    changed = policy.last_checked_value is not None and not policy_snapshot_matches(policy)
    if changed:
        session.add(PolicyChange(policy_id=policy.id, old_value=policy.last_checked_value, new_value=current))
        policy.verified = False
    policy.last_checked_value = current
    if not policy_is_valid(policy):
        policy.verified = False
        if verify:
            raise ValueError("검증 가능한 정책 규칙과 HTTPS 출처가 필요합니다.")
    elif verify:
        if not policy_source_current(policy, now or datetime.now(timezone.utc)):
            raise ValueError("최신 공식 정책 확인에 실패했습니다. 출처 점검 후 검증하세요.")
        policy.verified = True
        policy.observed_at = now or datetime.now(timezone.utc)
    if changed or verify or not policy.verified:
        session.execute(update(ReservationSchedule).where(ReservationSchedule.policy_id == policy.id)
                        .values(verified=False))
    return changed


def watch_matches_schedule(watch, schedule) -> bool:
    criteria = watch.criteria
    last = schedule.play_date_end or schedule.play_date
    days = (last - schedule.play_date).days
    return (0 <= days <= 6 and isinstance(criteria, dict)
            and any(criteria_includes_date(criteria, schedule.play_date + timedelta(days=offset)) for offset in range(days + 1))
            and criteria.get("audience", "all") in ("all", schedule.audience))


def watch_wants_reminder(watch, kind):
    values = watch.criteria.get("reminders", DEFAULT_REMINDERS) if isinstance(watch.criteria, dict) else ()
    return isinstance(values, (list, tuple)) and kind.removeprefix("opening_") in values


def reminder_kind(opens_at: datetime, now: datetime) -> str | None:
    opened = as_utc(opens_at)
    now = as_utc(now)
    if opened - timedelta(hours=1) <= now < opened:
        return "opening_hour_before"
    if opened - timedelta(days=1) <= now < opened - timedelta(hours=1):
        return "opening_day_before"
    if opened <= now < opened + OPENED_GRACE:
        return "opening_opened"
    return None


def enqueue_opening_notifications(session, schedule, users, kind, now) -> int:
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    count = 0
    for user_id in set(users):
        statement = insert(Notification).values(schedule_id=schedule.id, schedule_opens_at=schedule.opens_at,
            user_id=user_id, kind=kind, status="PENDING", attempts=0, next_attempt_at=now).on_conflict_do_update(
                index_elements=["schedule_id", "user_id", "kind", "schedule_opens_at"],
                set_={"status": "PENDING", "attempts": 0, "next_attempt_at": now, "last_error": None},
                where=Notification.status == "SKIPPED")
        count += len(session.execute(statement.returning(Notification.id)).scalars().all())
    return count


def check_opening_schedules(factory, *, now: datetime | None = None, activate_watches: bool = False) -> dict:
    now = as_utc(now or datetime.now(timezone.utc))
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    result = {"schedules": 0, "notifications": 0, "policy_changes": 0, "activated": 0}
    with factory.begin() as session:
        # Only subscribed, supported facilities are considered; no external HTTP/LLM calls.
        for facility in session.scalars(select(Facility).where(Facility.enabled.is_(True))):
            watches = eligible_watches(session, facility.id, "opening", now, ("PENDING_ACTIVATION", "ACTIVE"))
            if not watches:
                continue
            policies = list(session.scalars(select(ReservationPolicy).where(ReservationPolicy.facility_id == facility.id)))
            for policy in policies:
                result["policy_changes"] += int(track_policy_change(session, policy))
            by_audience = {}
            for policy in policies:
                by_audience.setdefault(policy.audience, []).append(policy)
            # The current rule model has no effective-version selector. Conflicting
            # same-audience rows must never be resolved arbitrarily into an alert.
            ambiguous = {audience for audience, rows in by_audience.items() if len(rows) > 1}
            for policy in policies:
                if policy.audience in ambiguous:
                    policy.verified = False
                    session.execute(update(ReservationSchedule).where(ReservationSchedule.policy_id == policy.id)
                                    .values(verified=False))
                    continue
                if not policy_is_valid(policy):
                    continue
                if not policy_source_current(policy, now):
                    session.execute(update(ReservationSchedule).where(ReservationSchedule.policy_id == policy.id)
                                    .values(verified=False))
                    continue
                from .policy_rules import play_dates_for_opening_horizon
                schedule_dates = play_dates_for_opening_horizon(today, policy)
                watch_dates = [day for day in schedule_dates if any(isinstance(watch.criteria, dict)
                    and criteria_includes_date(watch.criteria, day)
                    and watch.criteria.get("audience", "all") in ("all", policy.audience) for watch in watches)]
                result["schedules"] += calculate_schedules(session, facility.id, schedule_dates, [policy],
                    opens_after=now, opens_before=now + timedelta(days=366))
                if activate_watches and policy_can_notify(policy, now) and watch_dates:
                    for watch in watches:
                        if watch.monitoring_status == "PENDING_ACTIVATION" and any(
                            criteria_includes_date(watch.criteria, day) for day in watch_dates
                        ) and watch.criteria.get("audience", "all") in ("all", policy.audience):
                            watch.monitoring_status = "ACTIVE"
                            watch.updated_at = now
                            result["activated"] += 1
            for watch in watches:
                if not any(policy_can_notify(policy, now)
                    and watch.criteria.get("audience", "all") in ("all", policy.audience) for policy in policies):
                    if watch.monitoring_status == "ACTIVE":
                        watch.monitoring_status = "PENDING_ACTIVATION"
                        watch.updated_at = now
            session.flush()
            schedules = session.scalars(select(ReservationSchedule).where(
                ReservationSchedule.facility_id == facility.id, ReservationSchedule.verified.is_(True),
                ReservationSchedule.opens_at > now - OPENED_GRACE, ReservationSchedule.opens_at <= now + timedelta(days=1)))
            for schedule in schedules:
                policy = session.get(ReservationPolicy, schedule.policy_id)
                if policy is None or not policy_can_notify(policy, now) or not policy_snapshot_matches(policy):
                    continue
                kind = reminder_kind(schedule.opens_at, now)
                if kind:
                    users = [watch.user_id for watch in eligible_watches(session, facility.id, "opening", now)
                             if watch_matches_schedule(watch, schedule) and watch_wants_reminder(watch, kind)]
                    result["notifications"] += enqueue_opening_notifications(session, schedule, users, kind, now)
    return result
