from datetime import date
from unittest.mock import Mock

import httpx
import pytest

from tenniswatch.collectors.base import CollectionError, FacilityRef
from tenniswatch.collectors.gangseo import GangseoCollector, SUPPORTED_FACILITIES
from tenniswatch.collectors.policy import RobotsGuard

URL = "https://sports.gangseo.seoul.kr/fmcs/28"
FACILITY = FacilityRef(1, "우장산 1코트", "GANGSEO02:T:03:2", URL)
# Synthetic fixture. It is not a capture of the current site, which failed to load its selectors.
HTML = '<table><caption>예약 시간 선택</caption><tr><td>18:00~20:00</td><td>예약가능</td></tr></table>'


def test_robots_rules_are_checked_once_and_disallow_is_respected():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\nCrawl-delay: 7\n")
    guard = RobotsGuard("https://sports.gangseo.seoul.kr", transport=httpx.MockTransport(respond))
    guard.check(URL)
    guard.check(URL)
    with pytest.raises(CollectionError) as error:
        guard.check("https://sports.gangseo.seoul.kr/private")
    assert error.value.code == "ROBOTS_DISALLOWED" and len(requests) == 1
    assert guard.minimum_interval_seconds == 7


@pytest.mark.parametrize("status,body,allowed", [(404, "", True), (403, "", False), (503, "", False), (200, "<html>blocked</html>", False)])
def test_unusable_robots_response_stops_collection(status, body, allowed):
    guard = RobotsGuard("https://sports.gangseo.seoul.kr", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text=body)))
    if allowed:
        guard.check(URL)
    else:
        with pytest.raises(CollectionError, match="robots.txt"):
            guard.check(URL)


@pytest.mark.parametrize("html,code", [
    (HTML.replace("18:00~20:00", "20:00~18:00"), "INVALID_SLOT"),
    (HTML.replace("</table>", "<tr><td>18:00~20:00</td><td>마감</td></tr></table>"), "DUPLICATE_SLOT"),
])
def test_ambiguous_slot_table_is_rejected(html, code):
    with pytest.raises(CollectionError) as error:
        GangseoCollector._parse_slots(html, FACILITY, date(2026, 10, 24), "1코트", URL)
    assert error.value.code == code


def test_live_dom_date_and_court_must_match_before_parsing():
    guard = Mock(minimum_interval_seconds=0)
    collector = GangseoCollector(robots_guard=guard)
    page = Mock()
    page.goto.return_value = Mock(ok=True)
    page.url = URL
    page.content.return_value = HTML
    values = {"select#center": "GANGSEO02", "select#part": "03", "select#place": "2", 'input[name="base_date"]': "20261025"}
    page.locator.side_effect = lambda selector: Mock(input_value=lambda: values[selector])
    collector._page = page
    with pytest.raises(CollectionError) as error:
        list(collector.collect_slots(FACILITY, date(2026, 10, 24)))
    assert error.value.code == "PAGE_DATE_MISMATCH"
    page.content.assert_not_called()
    guard.check.assert_called_once()


def test_capabilities_do_not_claim_policy_or_site_discovery_support():
    capabilities = GangseoCollector.capabilities
    assert capabilities.slots and capabilities.requires_browser
    assert not capabilities.policies and not capabilities.facility_discovery and not capabilities.court_discovery
