"""Revalidate queued alerts before sending and fence all delivery acknowledgements."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .domain import SlotObservation, as_utc, next_reservation_open
from .models import (AvailabilityEvent, AvailabilitySlot, CollectionRun, DailyReportSetting, Facility, Notification,
                     ReservationPolicy, ReservationSchedule, ReservationSite, TelegramUser)
from .notifications import NotificationDeliveryError, opening_message, slot_message
from .opening import policy_can_notify, policy_snapshot_matches, reminder_kind, watch_matches_schedule, watch_wants_reminder
from .policy_rules import opening_window
from .services import claim_due_notifications, matching_subscriber_ids, record_delivery_failure
from .watches import eligible_watches


def release(row, status, code):
    row.status = status
    row.last_error = code
    row.locked_at = None
    row.lease_token = None


def defer(row, code, now, minutes):
    release(row, "RETRY", code)
    row.attempts = max(0, row.attempts - 1)
    row.next_attempt_at = now + timedelta(minutes=max(1, minutes))


def prepare_payload(session, row, now):
    user = session.get(TelegramUser, row.user_id)
    if user is None or not user.enabled:
        release(row, "SKIPPED", "USER_DISABLED")
        return None
    if row.kind == "daily_report":
        from .daily_reports import render_daily_report
        setting = session.get(DailyReportSetting, row.user_id)
        local = now.astimezone(ZoneInfo("Asia/Seoul"))
        if (not setting or not setting.enabled or row.report_revision != setting.revision
            or row.report_date != local.date()):
            release(row, "SKIPPED", "DAILY_REPORT_DISABLED_OR_EXPIRED")
            return None
        if local.hour*60+local.minute < setting.minute_of_day:
            defer(row, "DAILY_REPORT_NOT_DUE", now, 1)
            return None
        return render_daily_report(session, user.id, now), user.chat_id
    if row.kind == "slot_available":
        event = session.get(AvailabilityEvent, row.event_id) if row.event_id else None
        slot = session.get(AvailabilitySlot, event.slot_id) if event else None
        facility = session.get(Facility, slot.facility_id) if slot else None
    else:
        schedule = session.get(ReservationSchedule, row.schedule_id) if row.schedule_id else None
        facility = session.get(Facility, schedule.facility_id) if schedule else None
    site = session.get(ReservationSite, facility.site_id) if facility else None
    if facility is None or site is None:
        release(row, "FAILED", "MISSING_PAYLOAD")
        return None
    if not facility.enabled or not site.enabled or facility.collection_status != "SUPPORTED":
        release(row, "SKIPPED", "FACILITY_DISABLED")
        return None

    if row.kind == "slot_available":
        latest_event = session.scalar(select(AvailabilityEvent.id).where(AvailabilityEvent.slot_id == slot.id)
                                      .order_by(AvailabilityEvent.id.desc()).limit(1))
        if slot.status != "AVAILABLE" or event.current_status != "AVAILABLE" or latest_event != event.id:
            release(row, "SKIPPED", "AVAILABILITY_EVENT_SUPERSEDED")
            return None
        local_start = datetime.combine(slot.play_date, slot.starts_at.replace(tzinfo=None), ZoneInfo("Asia/Seoul"))
        if local_start <= now:
            release(row, "SKIPPED", "SLOT_STARTED")
            return None
        if slot.observed_at is None or as_utc(slot.observed_at) > now + timedelta(minutes=1) or now - as_utc(slot.observed_at) > timedelta(minutes=max(30, site.poll_minutes) * 2):
            defer(row, "STALE_OBSERVATION", now, site.poll_minutes)
            return None
        runs = session.scalars(select(CollectionRun).where(CollectionRun.facility_id == facility.id)
                               .order_by(CollectionRun.started_at.desc()).limit(100))
        recent = next((run for run in runs if run.requested_dates and slot.play_date.isoformat() in run.requested_dates), None)
        if recent and recent.status in ("ERROR", "PARTIAL") and as_utc(recent.started_at) > as_utc(slot.observed_at):
            defer(row, "COLLECTION_FAILED", now, site.poll_minutes)
            return None
        obs = SlotObservation(slot.slot_key, facility.id, slot.play_date, slot.starts_at, slot.ends_at,
                              slot.court_name, True, slot.booking_url, as_utc(slot.observed_at))
        if user.id not in matching_subscriber_ids(session, obs, now):
            release(row, "SKIPPED", "SUBSCRIPTION_NO_LONGER_MATCHES")
            return None
        return slot_message(facility=facility.name, play_date=slot.play_date.isoformat(),
            time_range=f"{slot.starts_at:%H:%M}~{slot.ends_at:%H:%M}", court=slot.court_name,
            booking_url=slot.booking_url, observed_at=as_utc(slot.observed_at)), user.chat_id

    policy = session.get(ReservationPolicy, schedule.policy_id)
    if (not schedule.verified or policy is None or not policy_can_notify(policy, now)
        or not policy_snapshot_matches(policy) or row.schedule_opens_at is None
        or as_utc(row.schedule_opens_at) != as_utc(schedule.opens_at)):
        release(row, "SKIPPED", "POLICY_OR_SCHEDULE_CHANGED")
        return None
    expected = opening_window(policy, schedule.play_date)
    if (as_utc(expected.opens_at) != as_utc(schedule.opens_at) or expected.first_date != schedule.play_date
            or expected.last_date != (schedule.play_date_end or schedule.play_date)):
        release(row, "SKIPPED", "POLICY_OR_SCHEDULE_CHANGED")
        return None
    if reminder_kind(schedule.opens_at, now) != row.kind:
        release(row, "SKIPPED", "REMINDER_WINDOW_EXPIRED")
        return None
    if not any(watch.user_id == user.id and watch_matches_schedule(watch, schedule)
               and watch_wants_reminder(watch, row.kind)
               for watch in eligible_watches(session, facility.id, "opening", now)):
        release(row, "SKIPPED", "SUBSCRIPTION_NO_LONGER_MATCHES")
        return None
    return opening_message(facility=facility.name, play_date=schedule.play_date.isoformat(),
        play_date_end=(schedule.play_date_end or schedule.play_date).isoformat(),
        audience=schedule.audience, opens_at=as_utc(schedule.opens_at), kind=row.kind,
        booking_url=facility.booking_url, source_url=policy.source_url, timezone_name=policy.timezone_name), user.chat_id


def dispatch_due_notifications(factory, sender, limit=50, *, now=None):
    fixed_now = as_utc(now) if now is not None else None
    now = fixed_now or datetime.now(timezone.utc)
    with factory.begin() as session:
        claims = [(row.id, row.lease_token) for row in claim_due_notifications(session, limit, now=now)]
    sent = failed = 0
    for notification_id, token in claims:
        now = fixed_now or datetime.now(timezone.utc)
        with factory.begin() as session:
            row = session.scalar(select(Notification).where(Notification.id == notification_id,
                Notification.status == "SENDING", Notification.lease_token == token).with_for_update())
            if row is None:
                continue
            row.locked_at = now
            payload = prepare_payload(session, row, now)
            if payload is None:
                failed += int(row.status == "FAILED")
                continue
        text, chat_id = payload
        try:
            sender.send(text, chat_id=chat_id)
        except Exception as exc:
            error = exc if isinstance(exc, NotificationDeliveryError) else NotificationDeliveryError("DELIVERY_FAILED")
            with factory.begin() as session:
                row = session.scalar(select(Notification).where(Notification.id == notification_id,
                    Notification.status == "SENDING", Notification.lease_token == token).with_for_update())
                if row is not None:
                    record_delivery_failure(row, error.code, now)
                    if error.permanent:
                        row.status = "FAILED"
                    if error.code == "TELEGRAM_403":
                        user = session.get(TelegramUser, row.user_id)
                        if user is not None:
                            user.enabled = False
                    if error.retry_after and row.status == "RETRY":
                        row.next_attempt_at = max(as_utc(row.next_attempt_at), now + timedelta(seconds=error.retry_after))
                    row.locked_at = None
                    row.lease_token = None
            failed += 1
        else:
            with factory.begin() as session:
                row = session.scalar(select(Notification).where(Notification.id == notification_id,
                    Notification.status == "SENDING", Notification.lease_token == token).with_for_update())
                if row is not None:
                    release(row, "SENT", None)
                    row.sent_at = now
                    sent += 1
    return sent, failed
