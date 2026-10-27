"""공식 공개 안내의 주간 예약 규칙만 수집한다. 로그인·달력·예약자 정보는 읽지 않는다."""
from dataclasses import dataclass
from datetime import time
import hashlib
import re
import time as clock

import httpx
from bs4 import BeautifulSoup

from .base import CollectionError
from .gyeyang import BASE_URL, ENTRY_PARAMS, GyeyangCollector
from .policy import RequestGuard, USER_AGENT

SOURCE_URL = GyeyangCollector.url(ENTRY_PARAMS)


@dataclass(frozen=True)
class WeeklyPolicyEvidence:
    open_weekday: int
    open_time: time
    window_start_days: int
    window_days: int
    digest: str
    source_url: str = SOURCE_URL

    @property
    def known_rule(self):
        return (self.open_weekday == 2 and self.open_time == time(14)
                and self.window_start_days == 1 and self.window_days == 7)


def parse_policy(html: str) -> WeeklyPolicyEvidence:
    text = re.sub(r"\s+", "", BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    sections = re.findall(r"시설대관이용안내\]?(.*?)(?:\[?부정예약금지|\[?사용요금안내)", text)
    if len(sections) != 1:
        raise CollectionError("POLICY_LAYOUT_CHANGED", "공식 예약 정책 안내를 확인하지 못했습니다.")
    section = sections[0]
    if "계양스포츠클럽회원만이용가능" not in section or "우선" in section:
        raise CollectionError("POLICY_REVIEW_REQUIRED", "회원 또는 우선 예약 조건을 다시 확인해야 합니다.")
    matches = re.findall(r"예약기간:매주([월화수목금토일])요일(\d{1,2})시(?:([0-5]?\d)분)?기준7일간"
        r"\(해당주([월화수목금토일])요일[~～]다음주([월화수목금토일])요일\)예약가능", section)
    if len(matches) != 1:
        raise CollectionError("POLICY_RULE_UNRECOGNIZED", "지원하는 주간 예약 규칙과 일치하지 않습니다.")
    day, hour, minute, start, end = matches[0]
    weekday = "월화수목금토일".index(day) + 1
    start_day = "월화수목금토일".index(start) + 1
    end_day = "월화수목금토일".index(end) + 1
    offset = (start_day - weekday) % 7
    if not offset or (start_day + 5) % 7 + 1 != end_day or int(hour) > 23:
        raise CollectionError("POLICY_RULE_UNRECOGNIZED", "주간 예약 대상 날짜 범위를 확인하지 못했습니다.")
    return WeeklyPolicyEvidence(weekday, time(int(hour), int(minute or 0)), offset, 7,
                                hashlib.sha256(section.encode()).hexdigest())


def fetch_policy(*, transport=None, request_guard=None) -> WeeklyPolicyEvidence:
    request_guard = request_guard or RequestGuard(BASE_URL)
    request_guard.check(SOURCE_URL)
    delay = 5
    clock.sleep(delay)
    try:
        with httpx.Client(transport=transport, timeout=20, follow_redirects=False,
                          headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(SOURCE_URL)
    except httpx.HTTPError:
        raise CollectionError("POLICY_CONNECTION_ERROR", "정책 출처 연결에 실패했습니다.") from None
    if response.status_code != 200:
        raise CollectionError("POLICY_HTTP_ERROR", "정책 출처가 정상 응답하지 않았습니다.")
    if "html" not in response.headers.get("content-type", "").lower() or len(response.content) > 2_000_000:
        raise CollectionError("POLICY_LAYOUT_CHANGED", "정책 출처의 문서 형식이 변경됐습니다.")
    return parse_policy(response.text)
