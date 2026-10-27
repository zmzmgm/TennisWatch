"""계양 공개 월간 달력 수집기. 빈 동적 선택 목록은 Playwright로 확인한다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import re
import time as clock
from urllib.parse import urlencode, urlsplit

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

from .base import BaseCollector, CollectionError, CollectorCapabilities, FacilityRef, safe_failure_details
from .policy import RequestGuard, USER_AGENT
from ..domain import SlotObservation

BASE_URL = "https://www.gysportsclub.or.kr"
BOOKING_URL = BASE_URL + "/res/"
ENTRY_PARAMS = {"rca_id": "10", "rca_id2": "1010", "rs_idx": "6"}
FACILITY_KEY = "gyeyang_tennis"
KST = timezone(timedelta(hours=9))
SLOT_RE = re.compile(r"^(\d{2}):00\s+(예약가능|예약완료|예약마감|예약불가|예약예정)(?:\s|$)")


def fail(code):
    raise CollectionError(code, "계양 공개 달력을 검증하지 못했습니다. 이전 예약 상태는 유지됩니다.")


def fail_unknown_status(text: str, *, disabled: bool = False) -> None:
    """Expose only a short status-like token, never the calendar row or reservation text."""
    error = CollectionError("UNKNOWN_STATUS", "계양 공개 달력의 예약 상태를 확인하지 못했습니다.")
    token = re.match(r"^\s*\d{2}:00\s+([^\s<>]{1,24})", text)
    diagnostics = {"disabled_while_marked_available": disabled}
    if token and re.fullmatch(r"[가-힣A-Za-z0-9_()/.-]{1,24}", token[1]):
        diagnostics["observed_status_token"] = token[1]
    else:
        diagnostics["status_token_available"] = False
    error.diagnostics = diagnostics
    raise error


def selector(soup, name):
    nodes = soup.select(f'select[name="{name}"], select#{name}')
    if len(nodes) != 1:
        fail("PAGE_LAYOUT_CHANGED")
    return nodes[0]


def selected_value(soup, name):
    control = selector(soup, name)
    options = control.select("option[selected]")
    # 단일 드롭다운은 selected 속성이 없으면 첫 활성 항목을 선택한다.
    # 요청값을 대신 사용하면 서버가 다른 코트로 돌려준 응답을 오인하므로 금지한다.
    if not options and not control.has_attr("multiple") and control.get("size", "1") in ("0", "1"):
        options = [option for option in control.find_all("option")
                   if not option.has_attr("disabled")
                   and not (option.parent.name == "optgroup" and option.parent.has_attr("disabled"))][:1]
    if len(options) != 1:
        fail("PAGE_SELECTION_MISMATCH")
    return options[0].get("value", "")


def selection_diagnostics(soup, params):
    """공개 선택값의 숫자와 개수만 제공한다. 본문·옵션 이름·스크립트는 출력 금지."""
    result = {}
    for name in ("rca_id", "rca_id2", "rs_idx"):
        if name not in params:
            continue
        controls = soup.select(f'select[name="{name}"], select#{name}')
        entry = {"control_count": len(controls)}
        if len(controls) == 1:
            options = controls[0].find_all("option")
            entry.update(option_count=len(options), explicit_selected_count=len(controls[0].select("option[selected]")))
            try:
                value = selected_value(soup, name)
            except CollectionError:
                value = None
            entry["selected_numeric_value"] = value if isinstance(value, str) and re.fullmatch(r"\d{1,10}", value) else None
        requested = params[name]
        entry["requested_numeric_value"] = requested if re.fullmatch(r"\d{1,10}", requested) else None
        result[name] = entry
    return result


@dataclass(frozen=True)
class CourtRef:
    category_id: str
    external_id: str
    name: str
    kind: str


class GyeyangCollector(BaseCollector):
    site_key = "gyeyang_incheon"
    capabilities = CollectorCapabilities(court_discovery=True, requires_browser=True)

    def __init__(self, *, transport=None, request_guard=None, min_interval_seconds=5, timeout_ms=25_000):
        self.guard = request_guard or RequestGuard(BASE_URL)
        self.transport = transport
        self.client = None
        self.interval = max(5, min_interval_seconds)
        self._last_request = None
        self._courts = None
        self._pages = {}
        self.timeout_ms = timeout_ms
        self._browser_mode = False
        self._playwright = None
        self._browser = None
        self._page = None

    def close(self):
        if self._page is not None:
            self._page.close()
            self._page = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        if self.client is not None:
            self.client.close()
            self.client = None

    @staticmethod
    def url(params):
        return BOOKING_URL + "?" + urlencode(params)

    def _before_request(self, url):
        self.guard.check(url)
        interval = max(self.interval, self.guard.minimum_interval_seconds)
        if self._last_request is not None:
            clock.sleep(max(0, interval - (clock.monotonic() - self._last_request)))
        self._last_request = clock.monotonic()

    @staticmethod
    def _validate_page(soup, params):
        if not all(text in soup.get_text(" ", strip=True) for text in ("계양", "시설대관")):
            fail("UNEXPECTED_PAGE")
        for name in ("rca_id", "rca_id2", "rs_idx"):
            if name not in params:
                continue
            try:
                if selected_value(soup, name) != params[name]:
                    fail("PAGE_SELECTION_MISMATCH")
            except CollectionError as exc:
                exc.diagnostics = selection_diagnostics(soup, params)
                raise

    @staticmethod
    def _needs_browser(soup):
        # 공개 초기 응답의 빈 동적 선택 목록일 때만 전환한다.
        # 이미 다른 코트가 명시적으로 선택된 경우는 불일치 오류로 중단한다.
        for name in ("rca_id2", "rs_idx"):
            control = selector(soup, name)
            values = [option.get("value", "") for option in control.find_all("option")]
            if not any(re.fullmatch(r"[1-9]\d*", value) for value in values):
                return True
        return False

    @staticmethod
    def _reflect_live_values(soup, values):
        for name in ("rca_id", "rca_id2", "rs_idx"):
            options = selector(soup, name).find_all("option")
            matches = [option for option in options if option.get("value", "") == values.get(name)]
            if len(matches) != 1 or not re.fullmatch(r"[1-9]\d*", values.get(name, "")):
                fail("PAGE_SELECTION_MISMATCH")
            for option in options:
                option.attrs.pop("selected", None)
            matches[0]["selected"] = ""

    @staticmethod
    def _read_snapshot(page):
        # 페이지의 전역 Object/Array 구현에 의존하는 직접 JS 실행을 피한다.
        # 읽기 전후의 실제 선택값이 같아야 HTML과 함께 사용할 수 있다.
        def read_values():
            return {name: page.locator(f'select[name="{name}"], select#{name}').input_value()
                    for name in ("rca_id", "rca_id2", "rs_idx")}
        before = read_values()
        html = page.content()
        after = read_values()
        if before != after:
            fail("PAGE_SELECTION_CHANGED")
        return {"html": html, "values": after}

    @staticmethod
    def _court_values(page):
        options = page.locator('select[name="rs_idx"], select#rs_idx').locator("option")
        return tuple(value for index in range(options.count())
                     if re.fullmatch(r"[1-9]\d*", value := options.nth(index).get_attribute("value") or ""))

    def _switch_category(self, params):
        """코트 번호 없는 URL로 이동하지 않고 공개 분류 선택 UI를 사용한다."""
        page = self._page
        stage = "switch_category"
        soup = None
        try:
            previous = self._court_values(page)
            category = page.locator('select[name="rca_id2"], select#rca_id2')
            changed = category.input_value() != params["rca_id2"]
            if changed:
                self._before_request(self.url(params))
                category.select_option(params["rca_id2"])
            deadline = clock.monotonic() + self.timeout_ms / 1000
            while True:
                values = self._court_values(page)
                if (category.input_value() == params["rca_id2"] and values
                        and (not changed or values != previous)):
                    break
                if clock.monotonic() >= deadline:
                    fail("CATEGORY_OPTIONS_TIMEOUT")
                clock.sleep(0.2)
            court = page.locator('select[name="rs_idx"], select#rs_idx')
            if court.input_value() not in values:
                stage = "select_discovered_court"
                self._before_request(self.url({**params, "rs_idx": values[0]}))
                court.select_option(values[0])
            actual = urlsplit(page.url)
            if actual.scheme != "https" or actual.netloc != urlsplit(BASE_URL).netloc or actual.path != "/res/":
                fail("UNEXPECTED_PAGE")
            stage = "category_snapshot"
            snapshot = self._read_snapshot(page)
            soup = BeautifulSoup(snapshot["html"], "html.parser")
            self._reflect_live_values(soup, snapshot["values"])
            self._validate_page(soup, params)
            return soup, datetime.now(timezone.utc)
        except CollectionError as exc:
            exc.diagnostics = selection_diagnostics(soup, params) if soup is not None else {}
            exc.diagnostics["stage"] = stage
            raise
        except Exception as exc:
            error = CollectionError("BROWSER_PROCESSING_FAILED", "계양 분류 전환 확인 중 오류가 발생했습니다.")
            error.diagnostics = {"stage": stage, **safe_failure_details(exc)}
            raise error from None

    def _get_browser(self, params):
        if "rs_idx" not in params:
            # 첫 공개 링크는 전체 선택값이 있어 정상 초기화된다.
            # 다른 분류의 첫 코트 ID는 추정하지 않고 UI에서 발견한다.
            self._get_browser(ENTRY_PARAMS)
            return self._switch_category(params)
        url = self.url(params)
        self._before_request(url)
        if self._page is None:
            try:
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(headless=True)
                self._page = self._browser.new_page(locale="ko-KR", timezone_id="Asia/Seoul",
                                                     user_agent=USER_AGENT)
                self._page.set_default_timeout(self.timeout_ms)
                self._page.set_default_navigation_timeout(self.timeout_ms)
            except Exception:
                self.close()
                fail("BROWSER_UNAVAILABLE")
        page = self._page
        stage = "navigation"
        soup = None
        values = {}
        try:
            response = page.goto(url, wait_until="domcontentloaded")
            if response is not None and response.status in (401, 403, 429):
                fail("ACCESS_RESTRICTED")
            if response is None or not response.ok:
                fail("HTTP_ERROR")
            actual = urlsplit(page.url)
            if actual.scheme != "https" or actual.netloc != urlsplit(BASE_URL).netloc or actual.path != "/res/":
                fail("UNEXPECTED_PAGE")
            stage = "wait_for_selectors"
            page.wait_for_function("""expected => ['rca_id','rca_id2','rs_idx'].every(name => {
                const nodes = document.querySelectorAll(`select[name="${name}"], select#${name}`);
                return nodes.length === 1 && /^[1-9]\\d*$/.test(nodes[0].value)
                    && (!Object.hasOwn(expected, name) || nodes[0].value === expected[name]);
            })""", arg=params, timeout=self.timeout_ms)
            stage = "snapshot"
            snapshot = self._read_snapshot(page)
            values = snapshot["values"]
            soup = BeautifulSoup(snapshot["html"], "html.parser")
            stage = "reflect_live_values"
            self._reflect_live_values(soup, values)
            stage = "validate_page"
            self._validate_page(soup, params)
            return soup, datetime.now(timezone.utc)
        except BrowserTimeout:
            error = CollectionError("PAGE_SELECTION_TIMEOUT", "계양 코트 목록 로딩 확인 시간이 초과됐습니다.")
            error.diagnostics = selection_diagnostics(BeautifulSoup(page.content(), "html.parser"), params)
            error.diagnostics["stage"] = stage
            raise error from None
        except CollectionError as exc:
            if soup is not None:
                diagnostics = selection_diagnostics(soup, params)
                for name in ("rca_id", "rca_id2", "rs_idx"):
                    if name not in diagnostics:
                        continue
                    value = values.get(name)
                    diagnostics[name]["live_numeric_value"] = value if isinstance(value, str) and re.fullmatch(r"\d{1,10}", value) else None
                    controls = soup.select(f'select[name="{name}"], select#{name}')
                    if len(controls) == 1:
                        diagnostics[name]["live_value_match_count"] = sum(
                            option.get("value", "") == value for option in controls[0].find_all("option"))
                diagnostics["stage"] = stage
                exc.diagnostics = diagnostics
            elif not hasattr(exc, "diagnostics"):
                exc.diagnostics = {"stage": stage}
            raise
        except Exception as exc:
            error = CollectionError("BROWSER_PROCESSING_FAILED", "계양 브라우저 응답 처리 중 오류가 발생했습니다.")
            error.diagnostics = {"stage": stage, **safe_failure_details(exc)}
            raise error from None

    def _get(self, params):
        if self._browser_mode:
            return self._get_browser(params)
        url = self.url(params)
        self._before_request(url)
        if self.client is None:
            self.client = httpx.Client(transport=self.transport, timeout=20, follow_redirects=False,
                                       headers={"User-Agent": USER_AGENT})
        try:
            response = self.client.get(url)
        except httpx.HTTPError:
            fail("CONNECTION_ERROR")
        if response.status_code in (401, 403, 429):
            fail("ACCESS_RESTRICTED")
        if response.status_code != 200:
            fail("HTTP_ERROR")
        if "html" not in response.headers.get("content-type", "").lower() or len(response.content) > 2_000_000:
            fail("UNEXPECTED_PAGE")
        soup = BeautifulSoup(response.text, "html.parser")
        if not all(text in soup.get_text(" ", strip=True) for text in ("계양", "시설대관")):
            fail("UNEXPECTED_PAGE")
        if self._needs_browser(soup):
            self._browser_mode = True
            return self._get_browser(params)
        self._validate_page(soup, params)
        return soup, datetime.now(timezone.utc)

    def discover_courts(self):
        """HTML이 실제 제공하는 선택값만 사용하며 3+3면 전수 확인을 요구한다."""
        if self._courts is not None:
            return self._courts
        soup, _ = self._get(ENTRY_PARAMS)
        category_options = selector(soup, "rca_id2").find_all("option")
        categories = [(o.get("value", ""), o.get_text(" ", strip=True)) for o in category_options
                      if re.fullmatch(r"\d+", o.get("value", "")) and
                      ("테니스" in o.get_text() or "실내" in o.get_text() or "실외" in o.get_text())]
        if not categories or len(categories) > 2:
            fail("COURT_INVENTORY_UNVERIFIED")
        courts = []
        for category_id, category_name in categories:
            if category_id == ENTRY_PARAMS["rca_id2"]:
                category_page = soup
            else:
                category_page, _ = self._get({"rca_id": ENTRY_PARAMS["rca_id"], "rca_id2": category_id})
            for option in selector(category_page, "rs_idx").find_all("option"):
                value = option.get("value", "")
                if not value or value == "0":
                    continue
                if not re.fullmatch(r"\d+", value):
                    fail("COURT_INVENTORY_UNVERIFIED")
                label = option.get_text(" ", strip=True)
                combined = category_name + " " + label
                kinds = [kind for kind in ("실내", "실외") if kind in combined]
                if len(kinds) != 1 or not label or len(label) > 80:
                    fail("COURT_INVENTORY_UNVERIFIED")
                courts.append(CourtRef(category_id, value, f"{kinds[0]} {label}" if kinds[0] not in label else label, kinds[0]))
        if (len({c.external_id for c in courts}) != 6 or len(courts) != 6
                or any(sum(c.kind == kind for c in courts) != 3 for kind in ("실내", "실외"))):
            fail("COURT_INVENTORY_UNVERIFIED")
        self._courts = tuple(courts)
        return self._courts

    @staticmethod
    def parse_day(soup, facility, court, day, url, observed_at):
        # 월 검증 없이 다른 달의 같은 날짜를 사용하지 않는다.
        month_label = re.compile(rf"(?<!\d){day.year}\s*[.년/-]\s*0?{day.month}(?:\s*월|\s*\.|(?!\d))")
        if not month_label.search(soup.get_text(" ", strip=True)):
            fail("PAGE_DATE_MISMATCH")
        tables = [t for t in soup.find_all("table")
                  if "해당월 요일별 일정" in " ".join((t.get_text(" ", strip=True),
                     str(t.get("summary", "")), str(t.get("aria-label", ""))))]
        if len(tables) != 1:
            fail("PAGE_LAYOUT_CHANGED")
        weekday = "월화수목금토일"[day.weekday()]
        cells = [cell for cell in tables[0].find_all("td")
                 if re.match(rf"^\s*{day.day}\s*\({weekday}\)", cell.get_text(" ", strip=True))]
        if len(cells) != 1:
            fail("PAGE_DATE_MISMATCH")
        items = cells[0].find_all("li")
        result = []
        hours = set()
        for item in items:
            text = item.get_text(" ", strip=True)
            match = SLOT_RE.match(text)
            if not match:
                fail_unknown_status(text)
            hour, status = int(match[1]), match[2]
            if hour not in range(6, 22) or hour in hours:
                fail("INVALID_SLOT")
            hours.add(hour)
            available = status == "예약가능"
            if available and (item.find(["del", "s", "strike"]) or
                              item.select('[disabled], [aria-disabled="true"]')):
                fail_unknown_status(text, disabled=True)
            result.append(SlotObservation(
                slot_key=f"{day.isoformat()}:{court.external_id}:{hour:02d}00-{hour+1:02d}00",
                facility_id=facility.id, play_date=day, starts_at=time(hour, tzinfo=KST),
                ends_at=time(hour+1, tzinfo=KST), court_name=court.name,
                court_external_id=court.external_id, available=available,
                booking_url=url, observed_at=observed_at))
        # 누락된 행을 마감으로 처리하지 않고 06~22시의 16행을 요구한다.
        if hours != set(range(6, 22)):
            fail("INCOMPLETE_SNAPSHOT")
        return result

    def collect_slots(self, facility, play_date):
        if facility.external_id != FACILITY_KEY:
            fail("UNSUPPORTED_FACILITY")
        observations = []
        for court in self.discover_courts():
            params = {"rca_id": ENTRY_PARAMS["rca_id"], "rca_id2": court.category_id, "rs_idx": court.external_id,
                      "year": str(play_date.year), "month": str(play_date.month)}
            key = (court.external_id, play_date.year, play_date.month)
            if key not in self._pages:
                self._pages[key] = self._get(params)
            soup, observed_at = self._pages[key]
            observations.extend(self.parse_day(soup, facility, court, play_date, self.url(params), observed_at))
        return observations
