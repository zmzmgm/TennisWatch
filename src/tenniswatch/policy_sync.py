"""구독된 시설의 공식 정책을 주기적으로 확인하고 변경 시 확정 알림을 보류한다."""
from datetime import datetime, timedelta, timezone
import time as clock

from sqlalchemy import select, update

from .collectors.base import CollectionError
from .collectors.gyeyang_policy import fetch_policy
from .domain import as_utc
from .models import Facility, PolicyCheckRun, ReservationPolicy, ReservationSchedule, ReservationSite
from .opening import track_policy_change
from .watches import eligible_watches

POLICY_FETCHERS = {"gyeyang_incheon": fetch_policy}
REFRESH_INTERVAL = timedelta(hours=6)


def refresh_policies(factory, *, now=None, facility_id=None, force=False, fetchers=None):
    now = as_utc(now or datetime.now(timezone.utc))
    fetchers = POLICY_FETCHERS if fetchers is None else fetchers
    with factory() as session:
        targets = [(facility.id, site.key) for facility, site in session.execute(
            select(Facility, ReservationSite).join(ReservationSite, Facility.site_id == ReservationSite.id)
            .where(Facility.enabled.is_(True), Facility.collection_status == "SUPPORTED", ReservationSite.enabled.is_(True)))
            if site.key in fetchers and (facility.id == facility_id if facility_id is not None else
                bool(eligible_watches(session, facility.id, "opening", now, ("PENDING_ACTIVATION", "ACTIVE"))))]
    result = {"policy_checks": 0, "policy_errors": 0, "review_required": 0}
    for target_id, site_key in targets:
        with factory() as session:
            last = session.scalar(select(PolicyCheckRun).where(PolicyCheckRun.facility_id == target_id)
                                  .order_by(PolicyCheckRun.id.desc()).limit(1))
            # 실패한 출처도 최소 1시간 간격으로 재확인한다.
            interval = timedelta(hours=1) if last and last.status == "ERROR" else REFRESH_INTERVAL
            if not force and last and now < as_utc(last.started_at) + interval:
                continue
        started = clock.monotonic()
        evidence = None
        error = None
        try:
            evidence = fetchers[site_key]()
        except Exception as exc:
            error = exc.code if isinstance(exc, CollectionError) else "POLICY_FETCH_FAILED"
        with factory.begin() as session:
            policies = list(session.scalars(select(ReservationPolicy).where(
                ReservationPolicy.facility_id == target_id, ReservationPolicy.audience == "general").with_for_update()))
            status = "VERIFIED"
            if len(policies) > 1:
                error = "AMBIGUOUS_POLICY"
            if error:
                for policy in policies:
                    policy.source_check_error = error
                    session.execute(update(ReservationSchedule).where(ReservationSchedule.policy_id == policy.id).values(verified=False))
                status = "ERROR"
                result["policy_errors"] += 1
            else:
                policy = policies[0] if policies else ReservationPolicy(facility_id=target_id, audience="general",
                    rule_type="weekly_batch", open_days_before=0, open_time=evidence.open_time,
                    timezone_name="Asia/Seoul", source_url=evidence.source_url, verified=False)
                is_new = not policies
                old_digest = policy.source_digest
                for field,value in {"rule_type":"weekly_batch", "open_days_before":0,
                    "open_weekday":evidence.open_weekday,"open_time":evidence.open_time,
                    "window_start_days":evidence.window_start_days,"window_days":evidence.window_days,
                    "source_digest":evidence.digest,"source_url":evidence.source_url,
                    "source_checked_at":now,"source_check_error":None}.items():
                    setattr(policy,field,value)
                session.add(policy)
                session.flush()
                # 최초 자동 검증은 공식 안내에서 확인한 화요일 14시 규칙에 한정한다.
                # 기존 정책 변경은 자동 재승인하지 않는다.
                changed = track_policy_change(session,policy,verify=is_new and evidence.known_rule,now=now)
                if not is_new and (changed or old_digest != evidence.digest):
                    policy.verified = False
                if not policy.verified:
                    status = "REVIEW_REQUIRED"
                    result["review_required"] += 1
            session.add(PolicyCheckRun(facility_id=target_id,started_at=now,
                duration_ms=int((clock.monotonic()-started)*1000),status=status,error_code=error))
        result["policy_checks"] += 1
    return result
