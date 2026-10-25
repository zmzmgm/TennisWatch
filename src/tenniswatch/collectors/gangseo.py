"""Read-only collector for the verified public Gangseo Wujang 1-court calendar.

The reservation form contains a CAPTCHA, but the public availability calendar is
rendered before it. This adapter never reads or submits the CAPTCHA form.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
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

# This identifier is taken from the site's public Wujang reservation link and was
# verified in the rendered page as 우장산 테니스장 > 1코트.
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
    capabilities = CollectorCapabilities(requires_browser=True)

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
        try:
            return SUPPORTED_FACILITIES[facility.external_id]
        except KeyError as exc:
            raise ValueError(
                f"Unverified Gangseo facility identifier: {facility.external_id!r}; "
                "only Wujang 1-court is currently verified."
            ) from exc

    @staticmethod
    def booking_url_for(external_id: str, play_date: date) -> str:
        try:
            config = SUPPORTED_FACILITIES[external_id]
        except KeyError as exc:
            raise ValueError(f"Unverified Gangseo facility identifier: {external_id!r}") from exc
        params = {key: value for key, value in config.items() if key != "court_name"}
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
                court_external_id=SUPPORTED_FACILITIES[facility.external_id]["place"],
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

    def collect_slots(self, facility: FacilityRef, play_date: date) -> Iterable[SlotObservation]:
        config = self._config(facility)
        url = self.booking_url_for(facility.external_id, play_date)
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
        return self._parse_slots(page.content(), facility, play_date,
                                 config["court_name"], url)
