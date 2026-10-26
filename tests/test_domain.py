from datetime import date, time, timezone, datetime
import pytest
from tenniswatch.domain import Availability, SlotObservation, next_reservation_open, should_notify
from tenniswatch.collectors.base import BaseCollector, CollectorRegistry, FacilityRef
from tenniswatch.collectors.gangseo import GangseoCollector


def test_open_date_is_timezone_aware_and_respects_resident_window():
    resident = next_reservation_open(date(2026, 10, 24), 7, time(9), timezone_name="Asia/Seoul")
    general = next_reservation_open(date(2026, 10, 24), 5, time(9), timezone_name="Asia/Seoul")
    assert resident.isoformat() == "2026-10-17T09:00:00+09:00"
    assert general.isoformat() == "2026-10-19T09:00:00+09:00"


def test_transition_notification_matrix():
    assert should_notify(Availability.UNKNOWN, Availability.AVAILABLE)
    assert should_notify(Availability.UNAVAILABLE, Availability.AVAILABLE)
    assert not should_notify(Availability.AVAILABLE, Availability.AVAILABLE)
    assert not should_notify(Availability.AVAILABLE, Availability.UNAVAILABLE)
    assert not should_notify(Availability.UNAVAILABLE, Availability.UNAVAILABLE)


def test_observation_requires_aware_timestamp():
    with pytest.raises(ValueError):
        SlotObservation("x", 1, date.today(), time(9), time(11), "court", True, "https://x", datetime.now())


def test_registry_and_unverified_gangseo_adapter():
    registry = CollectorRegistry()
    adapter = GangseoCollector()
    registry.register(adapter)
    assert registry.get("gangseo_seoul") is adapter
    with pytest.raises(ValueError, match="지원하지 않는 강서 시설"):
        list(adapter.collect_slots(FacilityRef(1, "court", "x", "https://sports.gangseo.seoul.kr/"), date.today()))
    with pytest.raises(ValueError):
        registry.register(adapter)


def test_gangseo_parser_uses_only_slot_time_and_availability():
    from tenniswatch.collectors.gangseo import GangseoCollector
    html = """
    <table><caption>예약 시간 선택</caption>
      <tr><th>선택</th><th>시간</th><th>요금</th><th>예약팀</th><th>예약자</th></tr>
      <tr><td>-</td><td>09:00~12:00</td><td>예약가능</td><td></td><td></td></tr>
      <tr><td>-</td><td>12:00~15:00</td><td>마감</td><td>민감 팀정보</td><td>민감 예약자정보</td></tr>
    </table>
    """
    facility = FacilityRef(7, "우장산 테니스장 1코트", "GANGSEO02:T:03:2", "https://sports.gangseo.seoul.kr/fmcs/28")
    slots = GangseoCollector._parse_slots(html, facility, date(2026, 10, 24), "1코트", facility.booking_url)
    assert [(slot.starts_at.replace(tzinfo=None), slot.ends_at.replace(tzinfo=None), slot.available) for slot in slots] == [
        (time(9), time(12), True), (time(12), time(15), False)
    ]
    assert all(slot.observed_at.tzinfo is not None for slot in slots)
    assert all(slot.starts_at.utcoffset().total_seconds() == 9 * 3600 for slot in slots)
    assert all("민감" not in slot.booking_url for slot in slots)


def test_gangseo_parser_fails_closed_on_unknown_page_or_status():
    from tenniswatch.collectors.gangseo import GangseoCollector
    facility = FacilityRef(7, "우장산 테니스장 1코트", "GANGSEO02:T:03:2", "https://sports.gangseo.seoul.kr/fmcs/28")
    with pytest.raises(ValueError, match="table was not found"):
        GangseoCollector._parse_slots("<html>captcha or error page</html>", facility, date.today(), "1코트", facility.booking_url)
    html = "<table><caption>예약 시간 선택</caption><tr><td>09:00~12:00</td><td>점검중</td></tr></table>"
    with pytest.raises(ValueError, match="Unrecognized availability"):
        GangseoCollector._parse_slots(html, facility, date.today(), "1코트", facility.booking_url)


def test_gangseo_collector_rejects_unverified_facilities():
    from tenniswatch.collectors.gangseo import GangseoCollector
    collector = GangseoCollector()
    with pytest.raises(ValueError, match="지원하지 않는 강서 시설"):
        collector._config(FacilityRef(7, "other", "UNKNOWN", "https://example.test"))


def test_gangseo_booking_url_uses_verified_court_and_site_date_format():
    from urllib.parse import parse_qs, urlsplit
    from tenniswatch.collectors.gangseo import GangseoCollector
    url = GangseoCollector.booking_url_for("GANGSEO02:T:03:2", date(2026, 10, 24))
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "sports.gangseo.seoul.kr"
    assert query["base_date"] == ["20261024"]
    assert query["place"] == ["2"]
    assert parsed.fragment == "regist_list"
