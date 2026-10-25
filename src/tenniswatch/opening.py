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


def policy_snapshot(policy: ReservationPolicy) -> str:
    return json.dumps({"audience": policy.audience, "open_days_before": policy.open_days_before,
        "open_time": policy.open_time.isoformat(), "timezone_name": policy.timezone_name,
        "source_url": policy.source_url}, sort_keys=True, ensure_ascii=False)


def policy_is_valid(policy: ReservationPolicy) -> bool:
    try:
        ZoneInfo(policy.timezone_name)
        source = urlsplit(policy.source_url)
        return (policy.audience in ("resident", "general") and 0 <= policy.open_days_before <= 365
                and source.scheme == "https" and bool(source.hostname) and not source.username and not source.password)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return False


def track_policy_change(session, policy: ReservationPolicy, *, verify: bool = False, now=None) -> bool:
    current = policy_snapshot(policy)
    changed = policy.last_checked_value is not None and policy.last_checked_value != current
    if changed:
        session.add(PolicyChange(policy_id=policy.id, old_value=policy.last_checked_value, new_value=current))
        policy.verified = False
    policy.last_checked_value = current
    if not policy_is_valid(policy):
        policy.verified = False
        if verify:
            raise ValueError("검증 가능한 정책 규칙과 HTTPS 출처가 필요합니다.")
    elif verify:
        policy.verified = True
        policy.observed_at = now or datetime.now(timezone.utc)
    if changed or verify or not policy.verified:
        session.execute(update(ReservationSchedule).where(ReservationSchedule.policy_id == policy.id)
                        .values(verified=False))
    return changed


def watch_matches_schedule(watch, schedule) -> bool:
    criteria = watch.criteria
    return (isinstance(criteria, dict) and criteria_includes_date(criteria, schedule.play_date)
            and criteria.get("audience", "all") in ("all", schedule.audience))


def reminder_kind(opens_at: datetime, now: datetime) -> str | None:
    opened = as_utc(opens_at)
    now = as_utc(now)
    if opened - timedelta(hours=1) <= now < opened:
        return "opening_hour_before"
    if opened - timedelta(days=1) <= now < opened - timedelta(hours=1):
        return "opening_day_before"
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
        count += session.execute(statement).rowcount or 0
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
                horizon = min(366, max(31, policy.open_days_before + 2))
                dates = [today + timedelta(days=offset) for offset in range(horizon + 1)]
                dates = [day for day in dates if any(isinstance(watch.criteria, dict)
                    and criteria_includes_date(watch.criteria, day)
                    and watch.criteria.get("audience", "all") in ("all", policy.audience) for watch in watches)]
                result["schedules"] += calculate_schedules(session, facility.id, dates, [policy])
                if activate_watches and policy.verified and dates:
                    for watch in watches:
                        if watch.monitoring_status == "PENDING_ACTIVATION" and any(
                            criteria_includes_date(watch.criteria, day) for day in dates
                        ) and watch.criteria.get("audience", "all") in ("all", policy.audience):
                            watch.monitoring_status = "ACTIVE"
                            watch.updated_at = now
                            result["activated"] += 1
            for watch in watches:
                if not any(policy.verified and policy_is_valid(policy)
                    and watch.criteria.get("audience", "all") in ("all", policy.audience) for policy in policies):
                    if watch.monitoring_status == "ACTIVE":
                        watch.monitoring_status = "PENDING_ACTIVATION"
                        watch.updated_at = now
            session.flush()
            schedules = session.scalars(select(ReservationSchedule).where(
                ReservationSchedule.facility_id == facility.id, ReservationSchedule.verified.is_(True),
                ReservationSchedule.opens_at > now, ReservationSchedule.opens_at <= now + timedelta(days=1)))
            for schedule in schedules:
                policy = session.get(ReservationPolicy, schedule.policy_id)
                if policy is None or not policy.verified or policy.last_checked_value != policy_snapshot(policy):
                    continue
                kind = reminder_kind(schedule.opens_at, now)
                if kind:
                    users = [watch.user_id for watch in eligible_watches(session, facility.id, "opening", now)
                             if watch_matches_schedule(watch, schedule)]
                    result["notifications"] += enqueue_opening_notifications(session, schedule, users, kind, now)
    return result
