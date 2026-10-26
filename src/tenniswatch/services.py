"""Transactional availability transitions and notification outbox helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .domain import Availability, SlotObservation, should_notify, as_utc, criteria_matches_slot
from .watches import eligible_watches
from uuid import uuid4
from .models import (
    AvailabilityEvent, AvailabilitySlot, Court, Facility, Notification, ReservationPolicy,
    ReservationSchedule, Subscription, TelegramUser,
)


@dataclass(frozen=True)
class TransitionResult:
    changed: bool
    event_id: int | None
    notification_created: bool


def apply_observation(session: Session, obs: SlotObservation) -> TransitionResult:
    """Persist a successful observation. Caller owns transaction and job lock."""
    slot = session.scalar(
        select(AvailabilitySlot)
        .where(AvailabilitySlot.facility_id == obs.facility_id,
               AvailabilitySlot.slot_key == obs.slot_key)
        .with_for_update()
    )
    previous = Availability(slot.status) if slot else Availability.UNKNOWN
    current = Availability.AVAILABLE if obs.available else Availability.UNAVAILABLE
    if slot and slot.observed_at:
        saved_at = slot.observed_at
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        if obs.observed_at <= saved_at:
            return TransitionResult(False, None, False)
    court_id = None
    if obs.court_external_id:
        court_id = session.scalar(select(Court.id).where(
            Court.facility_id == obs.facility_id, Court.external_id == obs.court_external_id))
    if slot is None:
        slot = AvailabilitySlot(
            facility_id=obs.facility_id, slot_key=obs.slot_key, court_name=obs.court_name,
            play_date=obs.play_date, starts_at=obs.starts_at, ends_at=obs.ends_at, status=current.value,
            booking_url=obs.booking_url, observed_at=obs.observed_at, court_id=court_id,
        )
        session.add(slot)
        session.flush()
    else:
        slot.status = current.value
        slot.court_name = obs.court_name
        slot.play_date = obs.play_date
        slot.starts_at = obs.starts_at
        slot.ends_at = obs.ends_at
        slot.booking_url = obs.booking_url
        slot.observed_at = obs.observed_at
        if court_id is not None:
            slot.court_id = court_id
    if current == previous:
        return TransitionResult(False, None, False)
    key = f"{obs.facility_id}:{obs.slot_key}:{obs.observed_at.isoformat()}:{current.value}"
    event = AvailabilityEvent(
        slot_id=slot.id, event_key=key, previous_status=previous.value,
        current_status=current.value, observed_at=obs.observed_at,
    )
    session.add(event)
    session.flush()
    return TransitionResult(True, event.id, should_notify(previous, current))


def _slot_matches_subscription(obs: SlotObservation, weekdays: str | None,
                               start_time, end_time) -> bool:
    if weekdays:
        try:
            allowed = {int(day.strip()) for day in weekdays.split(",") if day.strip()}
        except ValueError:
            return False
        # ISO weekdays: Monday=1 ... Sunday=7.
        if obs.play_date.isoweekday() not in allowed:
            return False
    # Subscription windows are local wall-clock preferences. Legacy rows may
    # contain naive time values while newly normalized slots include +09:00.
    slot_start = obs.starts_at.replace(tzinfo=None)
    start_time = start_time.replace(tzinfo=None) if start_time else None
    end_time = end_time.replace(tzinfo=None) if end_time else None
    if start_time and end_time:
        if start_time <= end_time:
            return start_time <= slot_start < end_time
        return slot_start >= start_time or slot_start < end_time
    if start_time:
        return slot_start >= start_time
    if end_time:
        return slot_start < end_time
    return True


def matching_subscriber_ids(session: Session, obs: SlotObservation, now: datetime | None = None) -> list[int]:
    """Resolve enabled facility subscriptions and apply weekday/time filters."""
    rows = session.execute(
        select(Subscription, TelegramUser)
        .join(TelegramUser, TelegramUser.id == Subscription.user_id)
        .where(Subscription.facility_id == obs.facility_id,
               Subscription.enabled.is_(True), TelegramUser.enabled.is_(True))
    ).all()
    users = {sub.user_id for sub, _user in rows
             if _slot_matches_subscription(obs, sub.weekdays, sub.start_time, sub.end_time)}
    now = now or datetime.now(timezone.utc)
    for watch in eligible_watches(session, obs.facility_id, "availability", now):
        if criteria_matches_slot(watch.criteria, obs.play_date, obs.starts_at):
            users.add(watch.user_id)
    return sorted(users)


def enqueue_event_notifications(session: Session, event_id: int, user_ids: list[int], *, now=None) -> int:
    """Idempotently add one outbox row per event and subscriber."""
    now = now or datetime.now(timezone.utc)
    count = 0
    for user_id in set(user_ids):
        values = dict(event_id=event_id, user_id=user_id, kind="slot_available",
                      status="PENDING", attempts=0, next_attempt_at=now)
        dialect = session.bind.dialect.name if session.bind is not None else ""
        if dialect == "postgresql":
            stmt = pg_insert(Notification).values(**values).on_conflict_do_update(
                index_elements=["event_id", "user_id", "kind"],
                set_={"status": "PENDING", "attempts": 0, "next_attempt_at": now, "last_error": None},
                where=Notification.status == "SKIPPED")
        elif dialect == "sqlite":
            stmt = sqlite_insert(Notification).values(**values).on_conflict_do_update(
                index_elements=["event_id", "user_id", "kind"],
                set_={"status": "PENDING", "attempts": 0, "next_attempt_at": now, "last_error": None},
                where=Notification.status == "SKIPPED")
        else:
            existing = session.scalar(select(Notification.id).where(
                Notification.event_id == event_id, Notification.user_id == user_id,
                Notification.kind == "slot_available"))
            if existing:
                continue
            session.add(Notification(**values))
            count += 1
            continue
        result = session.execute(stmt)
        count += result.rowcount or 0
    return count


def process_observation(session: Session, obs: SlotObservation, *, now=None) -> TransitionResult:
    """Queue the current availability event once per user, including new subscribers."""
    now = now or datetime.now(timezone.utc)
    result = apply_observation(session, obs)
    event_id = result.event_id if result.notification_created else None
    if event_id is None and obs.available:
        slot = session.scalar(select(AvailabilitySlot).where(
            AvailabilitySlot.facility_id == obs.facility_id, AvailabilitySlot.slot_key == obs.slot_key))
        if slot and slot.status == "AVAILABLE" and slot.observed_at and as_utc(slot.observed_at) == as_utc(obs.observed_at):
            event_id = session.scalar(select(AvailabilityEvent.id).where(
                AvailabilityEvent.slot_id == slot.id, AvailabilityEvent.current_status == "AVAILABLE")
                .order_by(AvailabilityEvent.id.desc()).limit(1))
    if event_id is not None:
        enqueue_event_notifications(session, event_id, matching_subscriber_ids(session, obs, now), now=now)
    return result

def calculate_schedules(session: Session, facility_id: int, play_dates: list, policies: list[ReservationPolicy]) -> int:
    """Insert/update schedules for known policy; unverified policies stay unverified."""
    from .policy_rules import opening_window
    count = 0
    for policy in policies:
        seen = set()
        for play_date in play_dates:
            window = opening_window(policy, play_date)
            if window.first_date in seen:
                continue
            seen.add(window.first_date)
            opened = window.opens_at.astimezone(timezone.utc)
            insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
            stmt = insert(ReservationSchedule).values(
                facility_id=facility_id, policy_id=policy.id, play_date=window.first_date,
                play_date_end=window.last_date,
                audience=policy.audience, opens_at=opened, verified=policy.verified,
            ).on_conflict_do_update(
                index_elements=["facility_id", "play_date", "audience"],
                set_={"policy_id": policy.id, "opens_at": opened, "verified": policy.verified,
                      "play_date_end": window.last_date},
            )
            session.execute(stmt)
            count += 1
    return count


def claim_due_notifications(session: Session, limit: int = 50, *, now: datetime | None = None) -> list[Notification]:
    """Claim due outbox rows. SKIP LOCKED lets concurrent workers divide the queue."""
    now = now or datetime.now(timezone.utc)
    lease_expired = Notification.locked_at <= now - timedelta(minutes=15)
    ready = or_(
        (Notification.status.in_(["PENDING", "RETRY"]) & (Notification.next_attempt_at <= now)),
        (Notification.status == "SENDING") & (lease_expired | Notification.locked_at.is_(None)),
    )
    session.execute(update(Notification).where(ready, Notification.attempts >= 8)
                    .values(status="FAILED", last_error="RETRY_EXHAUSTED", locked_at=None, lease_token=None))
    rows = list(session.scalars(select(Notification).where(ready, Notification.attempts < 8).order_by(Notification.id)
                                .limit(limit).with_for_update(skip_locked=True)))
    for row in rows:
        # Recover a worker crash after the 15-minute lease. Telegram has no idempotency
        # key, so a timeout after remote delivery can still result in a duplicate.
        row.status = "SENDING"
        row.attempts += 1
        row.locked_at = now
        row.lease_token = str(uuid4())
    return rows


def record_delivery_failure(notification: Notification, error: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    notification.status = "FAILED" if notification.attempts >= 8 else "RETRY"
    notification.last_error = error[:2000]
    notification.next_attempt_at = now + timedelta(minutes=min(2 ** notification.attempts, 360))


def dispatch_due_notifications(session_factory, sender, limit: int = 50, *, now: datetime | None = None) -> tuple[int, int]:
    from .delivery import dispatch_due_notifications as dispatch
    return dispatch(session_factory, sender, limit, now=now)
