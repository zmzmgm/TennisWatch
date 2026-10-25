"""Pure domain types and transition rules."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo


class Availability(StrEnum):
    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class SlotObservation:
    slot_key: str
    facility_id: int
    play_date: date
    starts_at: time
    ends_at: time
    court_name: str
    available: bool
    booking_url: str
    observed_at: datetime
    court_external_id: str | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.starts_at >= self.ends_at:
            raise ValueError("slot end must be after its start on the same local date")
        if self.facility_id <= 0 or not self.slot_key:
            raise ValueError("facility_id and slot_key are required")


def next_reservation_open(
    play_date: date,
    open_days_before: int,
    open_time: time,
    *,
    timezone_name: str = "Asia/Seoul",
) -> datetime:
    """Return local, timezone-aware opening datetime for a play date."""
    if open_days_before < 0:
        raise ValueError("open_days_before must be non-negative")
    local_day = play_date - timedelta(days=open_days_before)
    return datetime.combine(local_day, open_time, ZoneInfo(timezone_name))


def should_notify(previous: Availability, current: Availability) -> bool:
    return current is Availability.AVAILABLE and previous is not Availability.AVAILABLE


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def criteria_includes_date(criteria: dict, day: date) -> bool:
    if criteria.get("play_date"):
        return criteria["play_date"] == day.isoformat()
    weekdays = criteria.get("weekdays")
    if weekdays is None:
        return True
    return (isinstance(weekdays, list) and bool(weekdays)
            and all(type(item) is int and 1 <= item <= 7 for item in weekdays)
            and day.isoweekday() in weekdays)


def criteria_matches_slot(criteria: dict, day: date, starts_at: time) -> bool:
    if not criteria_includes_date(criteria, day):
        return False
    try:
        start = time.fromisoformat(criteria["start_time"]).replace(tzinfo=None) if criteria.get("start_time") else None
        end = time.fromisoformat(criteria["end_time"]).replace(tzinfo=None) if criteria.get("end_time") else None
    except (ValueError, TypeError):
        return False
    local = starts_at.replace(tzinfo=None)
    if start and end and end <= start:
        return False
    return (start is None or local >= start) and (end is None or local < end)
