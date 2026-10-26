"""연결된 사용자만 대상으로 오픈 알림 준비 상태를 조회한다."""
from datetime import datetime, timedelta, timezone
import json
from sqlalchemy import select

from .domain import as_utc
from .models import Facility, Notification, PolicyChange, ReservationPolicy, ReservationSchedule, TelegramUser, UserWatch
from .opening import policy_is_valid, policy_source_current, policy_snapshot_matches, watch_matches_schedule
from .watches import eligible_watches


def readiness_issues(policy, schedule, now):
    """발송 준비를 막는 조건을 안전한 코드로 설명한다."""
    issues = []
    if not schedule.verified:
        issues.append("SCHEDULE_UNVERIFIED")
    if policy is None:
        return issues + ["MISSING_POLICY"]
    if not policy.verified:
        issues.append("POLICY_UNVERIFIED")
    if not policy_is_valid(policy):
        issues.append("INVALID_POLICY_RULE")
    if not policy_source_current(policy, now):
        issues.append("POLICY_SOURCE_FAILED" if policy.source_check_error else "POLICY_SOURCE_NOT_CURRENT")
    if not policy_snapshot_matches(policy):
        issues.append("POLICY_SNAPSHOT_CHANGED")
    return issues


def policy_change_diagnostic(change):
    """비밀값이나 원문 대신 일정 계산 규칙의 변경만 표시한다."""
    if change is None:
        return None
    try:
        old, new = json.loads(change.old_value or '{}'), json.loads(change.new_value)
        if not isinstance(old, dict) or not isinstance(new, dict):
            return {"code": "INVALID_POLICY_HISTORY"}
    except (TypeError, ValueError):
        return {"code": "INVALID_POLICY_HISTORY"}
    safe = {"audience", "open_days_before", "open_time", "timezone_name", "rule_type",
            "open_weekday", "window_start_days", "window_days"}
    fields = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
    return {"changed_fields": fields,
            "rule_changes": {k: {"before": old.get(k), "after": new.get(k)} for k in fields if k in safe}}


def opening_status(factory, chat_id, *, now=None):
    now = as_utc(now or datetime.now(timezone.utc))
    result = {"linked_user": False, "opening_watches": 0, "pending_activation": 0,
              "upcoming": [], "notification_states": {}, "checked_at": now.isoformat()}
    if not chat_id:
        return result
    with factory() as session:
        user = session.scalar(select(TelegramUser).where(TelegramUser.chat_id == chat_id, TelegramUser.enabled.is_(True)))
        if user is None:
            return result
        result["linked_user"] = True
        watches = list(session.scalars(select(UserWatch).where(UserWatch.user_id == user.id,
            UserWatch.watch_type == "opening", UserWatch.enabled.is_(True))))
        watches = [w for w in watches if w.expires_at is None or as_utc(w.expires_at) > now]
        result["opening_watches"] = len(watches)
        result["pending_activation"] = sum(w.monitoring_status == "PENDING_ACTIVATION" for w in watches)
        for schedule in session.scalars(select(ReservationSchedule).where(
            ReservationSchedule.opens_at > now-timedelta(hours=3),
            ReservationSchedule.opens_at < now+timedelta(days=32)).order_by(ReservationSchedule.opens_at)):
            owned = [w for w in eligible_watches(session,schedule.facility_id,"opening",now,
                     ("ACTIVE","PENDING_ACTIVATION")) if w.user_id == user.id and watch_matches_schedule(w,schedule)]
            if not owned:
                continue
            policy = session.get(ReservationPolicy,schedule.policy_id)
            facility = session.get(Facility,schedule.facility_id)
            latest_change = session.scalar(select(PolicyChange).where(PolicyChange.policy_id == schedule.policy_id)
                                          .order_by(PolicyChange.id.desc()).limit(1))
            issues = readiness_issues(policy, schedule, now)
            result["upcoming"].append({"facility":facility.name,"audience":schedule.audience,
                "opens_at":as_utc(schedule.opens_at).isoformat(),"play_date":schedule.play_date.isoformat(),
                "play_date_end":(schedule.play_date_end or schedule.play_date).isoformat(),
                "policy_ready": not issues,
                "readiness_issues": issues,
                "schedule_verified": bool(schedule.verified),
                "policy_source_checked_at": as_utc(policy.source_checked_at).isoformat() if policy and policy.source_checked_at else None,
                "policy_id": schedule.policy_id,
                "policy_verified": bool(policy and policy.verified),
                "snapshot_matches": bool(policy and policy_snapshot_matches(policy)),
                "last_policy_change": policy_change_diagnostic(latest_change),
                "policy_error":policy.source_check_error if policy else "MISSING_POLICY"})
        for row in session.scalars(select(Notification).where(Notification.user_id==user.id,
            Notification.kind.in_(['opening_day_before','opening_hour_before','opening_opened']))
            .order_by(Notification.id.desc()).limit(100)):
            result["notification_states"][row.status] = result["notification_states"].get(row.status,0)+1
    return result
