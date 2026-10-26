"""Explicitly requested daily snapshots of a user's existing availability watches."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .domain import as_utc, criteria_includes_date, criteria_matches_slot
from .services import _slot_matches_subscription
from .models import (AvailabilitySlot, CollectionRun, DailyReportSetting, Facility, Notification,
    ReservationSite, Subscription, TelegramUser, UserWatch, UserWatchFacility)

KST = ZoneInfo('Asia/Seoul')


def next_daily_run(minute_of_day, now):
    if type(minute_of_day) is not int or not 0 <= minute_of_day < 1440:
        raise ValueError('시각은 00:00~23:59 범위여야 합니다.')
    local = as_utc(now).astimezone(KST)
    candidate = local.replace(hour=minute_of_day//60, minute=minute_of_day%60, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def configure_daily_report(session, user_id, minute_of_day, *, enabled=True, now=None):
    now = as_utc(now or datetime.now(timezone.utc))
    user = session.scalar(select(TelegramUser).where(TelegramUser.id == user_id).with_for_update())
    if not user or not user.enabled:
        raise ValueError('연결된 활성 Telegram 사용자가 필요합니다.')
    following = next_daily_run(minute_of_day, now)
    setting = session.scalar(select(DailyReportSetting).where(DailyReportSetting.user_id == user_id).with_for_update())
    if setting is None:
        setting = DailyReportSetting(user_id=user_id, minute_of_day=minute_of_day,
            enabled=enabled, next_run_at=following, updated_at=now)
        session.add(setting)
    elif setting.minute_of_day != minute_of_day or setting.enabled != enabled:
        setting.minute_of_day = minute_of_day
        setting.enabled = enabled
        setting.revision += 1
        setting.next_run_at = following
        setting.updated_at = now
    session.flush()
    return setting


def enqueue_daily_reports(factory, *, now=None):
    now = as_utc(now or datetime.now(timezone.utc))
    local = now.astimezone(KST)
    count = 0
    with factory.begin() as session:
        settings = list(session.scalars(select(DailyReportSetting)
            .join(TelegramUser, TelegramUser.id == DailyReportSetting.user_id)
            .where(DailyReportSetting.enabled.is_(True), TelegramUser.enabled.is_(True),
                DailyReportSetting.next_run_at <= now).with_for_update(skip_locked=True)))
        for setting in settings:
            due = local.replace(hour=setting.minute_of_day//60, minute=setting.minute_of_day%60, second=0, microsecond=0)
            if due > local:
                # Skip missed previous days; never send yesterday's snapshot today.
                setting.next_run_at = due.astimezone(timezone.utc)
                continue
            insert = pg_insert if session.bind.dialect.name == 'postgresql' else sqlite_insert
            result = session.execute(insert(Notification).values(user_id=setting.user_id, kind='daily_report',
                report_date=local.date(), report_revision=setting.revision, status='PENDING', attempts=0,
                next_attempt_at=now).on_conflict_do_nothing(index_elements=['user_id','kind','report_date'])
                .returning(Notification.id))
            # psycopg may return rowcount=-1; count the actual inserted IDs instead.
            count += int(result.scalar_one_or_none() is not None)
            setting.next_run_at = next_daily_run(setting.minute_of_day, now)
    return count


def _criteria_by_facility(session, user_id, now):
    conditions = {}
    for watch, facility_id in session.execute(select(UserWatch, UserWatchFacility.facility_id)
        .join(UserWatchFacility, UserWatch.id == UserWatchFacility.user_watch_id)
        .where(UserWatch.user_id == user_id, UserWatch.enabled.is_(True), UserWatch.watch_type == 'availability',
            UserWatch.monitoring_status.in_(['ACTIVE','PENDING_ACTIVATION']),
            or_(UserWatch.expires_at.is_(None), UserWatch.expires_at > now))):
        if isinstance(watch.criteria, dict):
            conditions.setdefault(facility_id, []).append(watch.criteria)
    for sub in session.scalars(select(Subscription).where(Subscription.user_id == user_id, Subscription.enabled.is_(True))):
        try:
            weekdays = [int(item.strip()) for item in sub.weekdays.split(',')] if sub.weekdays else None
            if weekdays and any(day not in range(1,8) for day in weekdays):
                continue
        except ValueError:
            continue
        conditions.setdefault(sub.facility_id, []).append({'weekdays': weekdays, '__legacy': True,
            'start_time': sub.start_time.strftime('%H:%M') if sub.start_time else None,
            'end_time': sub.end_time.strftime('%H:%M') if sub.end_time else None})
    return conditions


def _matches_slot(criteria, slot):
    if criteria.get('__legacy'):
        from datetime import time
        start = time.fromisoformat(criteria['start_time']) if criteria.get('start_time') else None
        end = time.fromisoformat(criteria['end_time']) if criteria.get('end_time') else None
        return criteria_includes_date(criteria, slot.play_date) and _slot_matches_subscription(slot, None, start, end)
    return criteria_matches_slot(criteria, slot.play_date, slot.starts_at)


def render_daily_report(session, user_id, now, *, days_ahead=7):
    """Re-read current conditions and freshness immediately before each delivery."""
    now = as_utc(now)
    today = now.astimezone(KST).date()
    dates = [today+timedelta(days=index) for index in range(days_ahead)]
    conditions = _criteria_by_facility(session, user_id, now)
    lines = [f'🎾 테니스 예약 가능 코트 — {today.isoformat()}',
        f'확인 기준: {now.astimezone(KST):%Y-%m-%d %H:%M} (한국 시간)', '기존 감시 조건 · 오늘부터 7일 이내']
    available = []
    unknown = []
    covered = 0
    omitted = 0
    for facility_id, filters in sorted(conditions.items()):
        facility = session.get(Facility, facility_id)
        site = session.get(ReservationSite, facility.site_id) if facility else None
        if not facility or not site or not facility.enabled or not site.enabled or facility.collection_status != 'SUPPORTED':
            unknown.append('비활성 또는 미지원 시설: 현재 예약 가능 여부 확인 불가')
            continue
        selected_days = [day for day in dates if any(criteria_includes_date(criteria, day) for criteria in filters)]
        for day in selected_days:
            covered += 1
            runs = session.scalars(select(CollectionRun).where(CollectionRun.facility_id == facility_id)
                .order_by(CollectionRun.started_at.desc()).limit(100))
            latest = next((run for run in runs if day.isoformat() in (run.requested_dates or [])), None)
            slots = list(session.scalars(select(AvailabilitySlot).where(AvailabilitySlot.facility_id == facility_id,
                AvailabilitySlot.play_date == day).order_by(AvailabilitySlot.starts_at, AvailabilitySlot.court_name)))
            relevant = [slot for slot in slots if any(_matches_slot(criteria, slot) for criteria in filters)
                and datetime.combine(day, slot.starts_at.replace(tzinfo=None), KST) > now]
            if not relevant:
                # A complete timetable with no requested/future rows can establish no matching slots.
                complete = latest and latest.status == 'SUCCESS' and latest.finished_at and slots
                fresh = complete and timedelta(0) <= now-as_utc(latest.finished_at) <= timedelta(minutes=max(30, site.poll_minutes)*2)
                if not fresh:
                    unknown.append(f'{facility.name} {day}: 최근 수집 정보 없음')
                continue
            if latest and latest.status in ('ERROR','PARTIAL','RUNNING') and any(
                slot.observed_at is None or as_utc(latest.started_at) > as_utc(slot.observed_at) for slot in relevant):
                unknown.append(f'{facility.name} {day}: 최근 수집 실패 또는 진행 중 · 현재 상태 확인 불가')
                continue
            for slot in relevant:
                fresh = slot.observed_at and timedelta(minutes=-1) <= now-as_utc(slot.observed_at) <= timedelta(minutes=max(30,site.poll_minutes)*2)
                if not fresh or slot.status not in ('AVAILABLE','UNAVAILABLE'):
                    unknown.append(f'{facility.name} {day}: 오래되었거나 미확인 상태')
                    continue
                if slot.status == 'AVAILABLE':
                    entry = (f'{facility.name} · {day} {slot.starts_at:%H:%M}~{slot.ends_at:%H:%M}\n'
                        f'{slot.court_name} · 예약 가능\n마지막 확인: {as_utc(slot.observed_at).astimezone(KST):%m-%d %H:%M}\n{slot.booking_url}')
                    if len(available) < 8:
                        available.append(entry)
                    else:
                        omitted += 1
    if available:
        lines += [''] + available
    elif not conditions or not covered:
        lines += ['', '조회 기간에 해당하는 감시 조건이 없습니다. 시설·날짜·시간 조건을 등록해 주세요.']
    elif unknown:
        lines += ['', '최근 확인 정보가 부족해 예약 가능한 코트 유무를 확정할 수 없습니다.']
    else:
        lines += ['', '최근 수집 결과에서 조건에 맞는 예약 가능한 코트가 없습니다.']
    if omitted:
        lines.append(f'예약 가능 슬롯 {omitted}개는 메시지 길이 제한으로 생략했습니다.')
    if unknown:
        lines += ['', '확인 제한:'] + list(dict.fromkeys(unknown))[:6]
    lines += ['', '관측 결과이며 예약 가능 여부는 변동될 수 있습니다. 링크에서 최종 확인해 주세요.']
    message = '\n'.join(lines)
    # Slot URLs/content are external data; keep within Telegram's message bound.
    if len(message) > 3500:
        message = message[:3300]+'\n일부 결과를 생략했습니다. 예약 사이트에서 최종 확인해 주세요.'
    return message
