"""예약 정책의 순수 계산 함수. 주간 오픈은 이용기간 하나로 묶는다."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .domain import next_reservation_open


@dataclass(frozen=True)
class OpeningWindow:
    opens_at: datetime
    first_date: date
    last_date: date


def rule_fields(policy):
    return {"rule_type": policy.rule_type or "rolling_days", "open_weekday": policy.open_weekday,
            "window_start_days": policy.window_start_days, "window_days": policy.window_days,
            "source_digest": policy.source_digest}


def rule_is_valid(policy):
    if (policy.rule_type or "rolling_days") == "rolling_days":
        return type(policy.open_days_before) is int and 0 <= policy.open_days_before <= 365
    return (policy.rule_type == "weekly_batch" and policy.open_days_before == 0
            and type(policy.open_weekday) is int and 1 <= policy.open_weekday <= 7
            and type(policy.window_start_days) is int and 1 <= policy.window_start_days <= 7
            and policy.window_days == 7)


def opening_window(policy, play_date: date) -> OpeningWindow:
    if not rule_is_valid(policy):
        raise ValueError("지원하지 않는 예약 오픈 규칙입니다.")
    if (policy.rule_type or "rolling_days") == "rolling_days":
        return OpeningWindow(next_reservation_open(play_date, policy.open_days_before, policy.open_time,
            timezone_name=policy.timezone_name), play_date, play_date)
    anchor = play_date - timedelta(days=policy.window_start_days)
    open_date = anchor - timedelta(days=(anchor.isoweekday() - policy.open_weekday) % 7)
    first = open_date + timedelta(days=policy.window_start_days)
    return OpeningWindow(datetime.combine(open_date, policy.open_time.replace(tzinfo=None),
        ZoneInfo(policy.timezone_name)), first, first + timedelta(days=6))
