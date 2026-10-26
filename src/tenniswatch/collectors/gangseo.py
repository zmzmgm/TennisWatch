"""강서 공개 예약 달력의 코트 발견 및 읽기 전용 잔여석 수집.

The reservation form contains a CAPTCHA, but the public availability calendar is
rendered before it. This adapter never reads or submits the CAPTCHA form.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from dataclasses import dataclass
import re
import time as clock
from typing import Iterable
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import Browser, Page, Playwright, TimeoutError as BrowserTimeout, sync_playwright

from .base import BaseCollector, CollectionError, CollectorCapabilities, FacilityRef
from .policy import RobotsGuard, USER_AGENT
from ..domain import SlotObservation


BASE_URL = "https://sports.gangseo.seoul.kr"
BOOKING_PATH = "/fmcs/28"

# 공식 /fmcs/64의 각 시설 '예약하기' 링크에서 확인했다. 번호를 순회하지 않는다.
FACILITY_CATALOG = {
    "GANGSEO02:T:03": {"name": "우장산근린공원 테니스장", "entry_place": "2"},
    "GANGSEO03:T:02": {"name": "강서구립테니스장", "entry_place": "3"},
    "GANGSEO05:C:02": {"name": "황금내근린공원 테니스장", "entry_place": "1"},
}


@dataclass(frozen=True)
class GangseoCourt:
    external_id: str
    facility_name: str
    court_name: str

    @property
    def name(self):
        return f"{self.facility_name} {self.court_name}"


def config_for_id(external_id: str) -> dict[str, str]:
    parts = external_id.split(":")
    if len(parts) != 4 or ":".join(parts[:3]) not in FACILITY_CATALOG or not re.fullmatch(r"[1-9][0-9]{0,7}", parts[3]):
        raise ValueError("지원하지 않는 강서 시설 식별자입니다.")
    return dict(zip(("center", "facilities_type", "part", "place"), parts), rent_type="1001")


def parse_court_options(options, group: str) -> tuple[GangseoCourt, ...]:
    """실제 선택 목록만 받아 파싱한다. 예시 번호나 물리적 면수로 채우지 않는다."""
    if group not in FACILITY_CATALOG:
        raise CollectionError("UNSUPPORTED_FACILITY", "지원하지 않는 강서 시설입니다.")
    courts = []
    seen = set()
    labels = set()
    for option in options:
        value, label = str(option.get("value", "")), str(option.get("label", "")).strip()
        if not value or option.get("disabled"):
            continue
        if value in ("0", "-1") and "선택" in label:
            continue
        if (not re.fullmatch(r"[1-9][0-9]{0,7}", value) or not label or len(label) > 80
            or any(word in label for word in ("불러오기", "오류", "선택", "없음"))
            or value in seen or label in labels):
            raise CollectionError("COURT_INVENTORY_UNVERIFIED", "코트 선택 목록을 확인하지 못했습니다.")
        seen.add(value)
        labels.add(label)
        courts.append(GangseoCourt(f"{group}:{value}", FACILITY_CATALOG[group]["name"], label))
    if not 1 <= len(courts) <= 20:
        raise CollectionError("COURT_INVENTORY_UNVERIFIED", "코트 선택 목록이 비어 있거나 변경됐습니다.")
    validate_court_count(group, len(courts))
    return tuple(courts)


def validate_court_count(group: str, count: int) -> None:
    # 사용자가 요청한 우장산 전체 4면을 일부 목록으로 등록하지 않는다.
    if group == "GANGSEO02:T:03" and count != 4:
        error = CollectionError("COURT_INVENTORY_INCOMPLETE", "우장산 4개 코트 목록을 모두 확인해야 합니다.")
        error.diagnostics = {"facility_group": group, "expected_count": 4, "found_count": count}
        raise error

# 기존 외부 사용자의 import 호환용 1코트 상수다. 전체 지원 범위는 FACILITY_CATALOG와
# 실제 발견한 선택 목록을 사용하며, 여기에 다른 코트 번호를 추정해 채우지 않는다.
SUPPORTED_FACILITIES = {
    "GANGSEO02:T:03:2": {
        "center": "GANGSEO02",
        "facilities_type": "T",
        "part": "03",
        "place": "2",
        "rent_type": "1001",
        "court_name": "1코트",
    }
}

TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})\s*~\s*(\d{1,2}):(\d{2})(?!\d)")
AVAILABLE_LABELS = {"예약가능"}
UNAVAILABLE_LABELS = {"마감", "예약완료", "예약불가"}
KST = timezone(timedelta(hours=9))


class GangseoCollector(BaseCollector):
    site_key = "gangseo_seoul"
    capabilities = CollectorCapabilities(requires_browser=True, court_discovery=True)

    def __init__(self, *, timeout_ms: int = 25_000, min_interval_seconds: float = 5,
                 robots_guard: RobotsGuard | None = None) -> None:
        self.timeout_ms = timeout_ms
        self.min_interval_seconds = max(5, min_interval_seconds)
        self.robots_guard = robots_guard or RobotsGuard(BASE_URL)
        self._last_navigation: float | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    def start(self) -> None:
        """Start one reusable headless browser for a batch of dates."""
        if self._browser is not None:
            return
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
            self._page = self._browser.new_page(locale="ko-KR", timezone_id="Asia/Seoul",
                                                 user_agent=USER_AGENT)
            self._page.set_default_timeout(self.timeout_ms)
            self._page.set_default_navigation_timeout(self.timeout_ms)
        except Exception as exc:
            self.close()
            raise CollectionError("BROWSER_UNAVAILABLE", "Playwright Chromium could not be started; check its installation.") from exc

    def close(self) -> None:
        if self._page is not None:
            self._page.close()
            self._page = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def __enter__(self) -> "GangseoCollector":
        # Browser launch is lazy, after the robots check and adapter validation.
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _config(self, facility: FacilityRef) -> dict[str, str]:
        return config_for_id(facility.external_id)

    @staticmethod
    def booking_url_for(external_id: str, play_date: date | None = None) -> str:
        params = config_for_id(external_id)
        if play_date is not None:
            params["base_date"] = play_date.strftime("%Y%m%d")
        return f"{BASE_URL}{BOOKING_PATH}?{urlencode(params)}#regist_list"

    @staticmethod
    def _parse_slots(html: str, facility: FacilityRef, play_date: date,
                     court_name: str, booking_url: str) -> list[SlotObservation]:
        soup = BeautifulSoup(html, "html.parser")
        table = next((candidate for candidate in soup.find_all("table")
                      if "예약 시간 선택" in " ".join([
                          candidate.get_text(" ", strip=True),
                          str(candidate.get("summary", "")),
                          str(candidate.get("aria-label", "")),
                          str(candidate.get("title", "")),
                      ])), None)
        if table is None:
            raise CollectionError("PAGE_LAYOUT_CHANGED", "Gangseo availability table was not found; page layout may have changed.")

        observations: list[SlotObservation] = []
        unknown_rows = False
        observed_at = datetime.now(timezone.utc)
        seen_keys: set[str] = set()
        for row in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            joined = " ".join(cells)
            match = TIME_RE.search(joined)
            if not match:
                continue
            start_hour, start_minute, end_hour, end_minute = map(int, match.groups())
            status = next((text.strip() for text in cells
                           if text.strip() in AVAILABLE_LABELS | UNAVAILABLE_LABELS), None)
            if status is None:
                unknown_rows = True
                continue
            if status in AVAILABLE_LABELS:
                available = True
            elif status in UNAVAILABLE_LABELS:
                available = False
            else:  # defensive if labels change
                unknown_rows = True
                continue
            try:
                starts_at = time(start_hour, start_minute, tzinfo=KST)
                ends_at = time(end_hour, end_minute, tzinfo=KST)
            except ValueError as exc:
                raise CollectionError("INVALID_SLOT", "Gangseo table contains an invalid time range.") from exc
            if starts_at >= ends_at or len(TIME_RE.findall(joined)) != 1:
                raise CollectionError("INVALID_SLOT", "Gangseo table contains an ambiguous time range.")
            slot_key = f"{play_date.isoformat()}:{court_name}:{starts_at:%H%M}-{ends_at:%H%M}"
            if slot_key in seen_keys:
                raise CollectionError("DUPLICATE_SLOT", "Gangseo table contains duplicate time slots.")
            seen_keys.add(slot_key)
            observations.append(SlotObservation(
                slot_key=slot_key,
                facility_id=facility.id,
                play_date=play_date,
                starts_at=starts_at,
                ends_at=ends_at,
                court_name=court_name,
                available=available,
                booking_url=booking_url,
                observed_at=observed_at,
                court_external_id=config_for_id(facility.external_id)["place"],
            ))
        if unknown_rows:
            raise CollectionError("UNKNOWN_STATUS", "Unrecognized availability status in Gangseo table; refusing partial data.")
        if not observations:
            raise CollectionError("NO_SLOT_DATA", "Gangseo availability table contained no recognizable time slots.")
        return observations

    @staticmethod
    def _validate_context(selected: dict[str, str], actual_date: str,
                          config: dict[str, str], play_date: date) -> None:
        if selected != {field: config[field] for field in ("center", "part", "place")}:
            raise CollectionError("PAGE_SELECTION_MISMATCH", "Gangseo selected facility/court could not be verified.")
        if actual_date.replace("-", "") != play_date.strftime("%Y%m%d"):
            raise CollectionError("PAGE_DATE_MISMATCH", "Gangseo selected date could not be verified.")

    def _navigate(self, url: str):
        self.robots_guard.check(url)
        interval = max(self.min_interval_seconds, self.robots_guard.minimum_interval_seconds)
        if interval > 60:
            raise CollectionError("ROBOTS_RATE_LIMIT", "The site's request interval requires deferred collection.")
        if self._page is None:
            self.start()
        assert self._page is not None
        page = self._page
        if self._last_navigation is not None:
            clock.sleep(max(0, interval - (clock.monotonic() - self._last_navigation)))
        self._last_navigation = clock.monotonic()
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except BrowserTimeout as exc:
            raise CollectionError("PAGE_TIMEOUT", "Gangseo calendar navigation timed out.") from exc
        if response is not None and response.status in (401, 403, 429):
            raise CollectionError("ACCESS_RESTRICTED", "Gangseo refused or rate-limited the calendar request.")
        if response is None or not response.ok:
            raise CollectionError("HTTP_ERROR", "Gangseo calendar did not return a successful response.")
        if urlsplit(page.url).netloc != urlsplit(BASE_URL).netloc or urlsplit(page.url).path != BOOKING_PATH:
            raise CollectionError("UNEXPECTED_PAGE", "Gangseo redirected away from the public calendar.")
        return page

    def _select_discovery_context(self, page, config):
        """공개 선택 목록을 센터→종목→코트 순서로 최대 한 번씩 선택한다."""
        for field in ("center", "part", "place"):
            try:
                page.wait_for_function("""({field, value}) => {
                    const select = document.querySelector('select#' + field);
                    return select && !select.disabled && Array.from(select.options).some(
                        option => !option.disabled && option.value === value);
                }""", arg={"field": field, "value": config[field]}, timeout=self.timeout_ms)
                # 이미 선택돼 있어도 change를 한 번 발생시켜 하위 목록의 초기화를 요청한다.
                interval = max(self.min_interval_seconds, self.robots_guard.minimum_interval_seconds)
                if interval > 60:
                    raise CollectionError("ROBOTS_RATE_LIMIT", "요청 간격 제한으로 조회를 중단했습니다.")
                if self._last_navigation is not None:
                    clock.sleep(max(0, interval - (clock.monotonic() - self._last_navigation)))
                self._last_navigation = clock.monotonic()
                page.locator(f"select#{field}").select_option(config[field], timeout=self.timeout_ms)
            except BrowserTimeout:
                error = CollectionError("COURT_DISCOVERY_TIMEOUT", "시설 선택 목록을 확인하지 못했습니다.")
                error.diagnostics = {"facility_group": ":".join(config[k] for k in ("center", "facilities_type", "part")),
                                     "stage": field + "_options"}
                try:
                    error.diagnostics["controls"] = page.evaluate("""expected =>
                        Object.fromEntries(['center','part','place'].map(id => {
                            const controls = document.querySelectorAll('select#' + id);
                            const control = controls[0];
                            const options = Array.from(control?.options || []);
                            return [id, {control_count: controls.length, option_count: options.length,
                                disabled: Boolean(control?.disabled),
                                expected_selected: control?.value === expected[id],
                                expected_option_present: options.some(o => o.value === expected[id]),
                                enabled_option_count: options.filter(o => !o.disabled && /^[1-9][0-9]*$/.test(o.value)).length}];
                        }))""", config)
                except Exception:
                    error.diagnostics["diagnostics_unavailable"] = True
                raise error from None

    def discover_courts(self, *, groups=None) -> tuple[GangseoCourt, ...]:
        """공식 세 시설의 선택 목록을 모두 검증한 뒤 결과를 반환한다."""
        selected_groups = tuple(FACILITY_CATALOG) if groups is None else tuple(groups)
        if not selected_groups or len(set(selected_groups)) != len(selected_groups) or any(g not in FACILITY_CATALOG for g in selected_groups):
            raise ValueError("지원하는 시설 그룹을 지정하세요.")
        result = []
        for group in selected_groups:
            info = FACILITY_CATALOG[group]
            external_id = f"{group}:{info['entry_place']}"
            config = config_for_id(external_id)
            page = self._navigate(self.booking_url_for(external_id))
            self._select_discovery_context(page, config)
            snapshot = page.evaluate("""() => ({
                selected: Object.fromEntries(['center','part'].map(id => [id, document.getElementById(id)?.value])),
                options: Array.from(document.querySelectorAll('select#place option')).map(
                  option => ({value: option.value, label: option.textContent, disabled: option.disabled}))})""")
            if snapshot["selected"] != {key: config[key] for key in ("center", "part")}:
                raise CollectionError("PAGE_SELECTION_MISMATCH", "발견 중 시설 선택이 변경됐습니다.")
            courts = parse_court_options(snapshot["options"], group)
            if not any(item.external_id == external_id for item in courts):
                raise CollectionError("COURT_INVENTORY_UNVERIFIED", "공식 링크의 코트가 선택 목록에 없습니다.")
            result.extend(courts)
        return tuple(result)

    def collect_slots(self, facility: FacilityRef, play_date: date) -> Iterable[SlotObservation]:
        config = self._config(facility)
        url = self.booking_url_for(facility.external_id, play_date)
        page = self._navigate(url)
        try:
            page.wait_for_function("""expected => ['center','part','place'].every(
                id => document.getElementById(id)?.value === expected[id])""", arg=config,
                timeout=self.timeout_ms)
        except BrowserTimeout as exc:
            raise CollectionError("PAGE_SELECTION_MISMATCH", "Gangseo facility selectors did not finish loading.") from exc
        table = page.get_by_role("table", name=re.compile("예약 시간 선택"))
        try:
            table.locator("tr").filter(has_text=TIME_RE).first.wait_for(state="visible", timeout=self.timeout_ms)
        except BrowserTimeout as exc:
            raise CollectionError("NO_SLOT_DATA", "Gangseo slot rows did not finish loading.") from exc
        # JS property changes are not necessarily reflected by HTML value/selected attributes.
        selected = {field: page.locator(f"select#{field}").input_value() for field in ("center", "part", "place")}
        actual_date = page.locator('input[name="base_date"]').input_value().replace("-", "")
        self._validate_context(selected, actual_date, config, play_date)
        # 선택값과 시간표를 같은 DOM 실행에서 읽어 AJAX 갱신 사이의 혼합을 막는다.
        snapshot = page.evaluate("""() => ({
            selected: Object.fromEntries(['center','part','place'].map(id => [id, document.getElementById(id)?.value])),
            actual_date: document.querySelector('input[name="base_date"]')?.value || '',
            court_name: document.getElementById('place')?.selectedOptions[0]?.textContent || '',
            html: Array.from(document.querySelectorAll('table')).find(table =>
              [table.textContent, table.getAttribute('summary'), table.getAttribute('aria-label'), table.title]
              .join(' ').includes('예약 시간 선택'))?.outerHTML || ''})""")
        self._validate_context(snapshot["selected"], snapshot["actual_date"], config, play_date)
        court_name = snapshot["court_name"].strip()
        if not court_name or len(court_name) > 80:
            raise CollectionError("COURT_INVENTORY_UNVERIFIED", "선택한 코트 이름을 확인하지 못했습니다.")
        return self._parse_slots(snapshot["html"], facility, play_date,
                                 court_name, url)
