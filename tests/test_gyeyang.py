"""모의 HTML 계약 테스트. 실제 사이트 화면을 캡처한 자료가 아니다."""
from datetime import date, datetime, timezone
from unittest.mock import Mock

from bs4 import BeautifulSoup
import httpx
import pytest

from tenniswatch.collectors.base import CollectionError, FacilityRef
from tenniswatch.collectors.gyeyang import CourtRef, GyeyangCollector, FACILITY_KEY, BOOKING_URL

DAY = date(2026, 10, 26)
FACILITY = FacilityRef(1, "계양테니스장", FACILITY_KEY, BOOKING_URL)
COURT = CourtRef("1010", "6", "실내 시험코트", "실내")
OBSERVED = datetime(2026, 10, 26, tzinfo=timezone.utc)


def html(status="예약가능", court="6", category="1010"):
    # 식별자와 코트 이름은 전부 테스트 자료다.
    options = ''.join(f'<option value="{i}" {"selected" if str(i)==court else ""}>{"실내" if i<9 else "실외"} 시험{i}</option>' for i in range(6,12))
    rows = ''.join(f'<li>{hour:02}:00 {status}</li>' for hour in range(6,22))
    return f'''<h1>계양 시설대관</h1><select name="rca_id"><option selected value="10">테니스</option></select>
    <select name="rca_id2"><option selected value="{category}">테니스</option></select>
    <select name="rs_idx">{options}</select><h2>2026.10</h2>
    <table><caption>해당월 요일별 일정</caption><tr><td>26(월)<ul>{rows}</ul></td></tr></table>'''


def parse(source):
    return GyeyangCollector.parse_day(BeautifulSoup(source,"html.parser"), FACILITY, COURT,
                                     DAY, BOOKING_URL, OBSERVED)


@pytest.mark.parametrize("label,available", [("예약가능",True),("예약완료",False),
    ("예약마감",False),("예약불가",False),("예약예정",False)])
def test_only_explicit_available_label_is_available(label, available):
    slots = parse(html(label))
    assert len(slots) == 16 and all(s.available is available for s in slots)
    assert slots[0].observed_at == OBSERVED and slots[0].starts_at.utcoffset() is not None


@pytest.mark.parametrize("change,code", [
    (lambda x:x.replace("2026.10","2026.11"),"PAGE_DATE_MISMATCH"),
    (lambda x:x.replace("26(월)","27(화)"),"PAGE_DATE_MISMATCH"),
    (lambda x:x.replace("예약가능","예약신청"),"UNKNOWN_STATUS"),
    (lambda x:x.replace('<li>06:00 예약가능</li>',""),"INCOMPLETE_SNAPSHOT"),
    (lambda x:x.replace('<li>06:00 예약가능</li>','<li>06:00 예약가능</li>'*2),"INVALID_SLOT"),
    (lambda x:x.replace('06:00 예약가능','06:00 <del>예약가능</del>'),"UNKNOWN_STATUS"),
])
def test_incomplete_ambiguous_pages_never_become_observations(change, code):
    with pytest.raises(CollectionError) as error:
        parse(change(html()))
    assert error.value.code == code


def collector(monkeypatch, respond):
    monkeypatch.setattr("tenniswatch.collectors.gyeyang.clock.sleep", lambda _:None)
    guard = Mock(minimum_interval_seconds=0)
    return GyeyangCollector(transport=httpx.MockTransport(respond), robots_guard=guard)


def test_discover_all_courts_and_cache_month_without_retimestamping(monkeypatch):
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,text=html(court=request.url.params.get("rs_idx","6")),headers={"content-type":"text/html"})
    with collector(monkeypatch, respond) as adapter:
        slots = adapter.collect_slots(FACILITY, DAY)
        again = adapter.collect_slots(FACILITY, DAY)
        assert len(slots)==96 and len(adapter.discover_courts())==6
        assert len(requests)==7 and slots==again
        assert len({s.slot_key for s in slots})==96


def test_partial_court_inventory_is_rejected(monkeypatch):
    with collector(monkeypatch, lambda _:httpx.Response(200,text=html().replace('value="11"','value=""'),headers={"content-type":"text/html"})) as adapter:
        with pytest.raises(CollectionError) as error:
            adapter.discover_courts()
        assert error.value.code=="COURT_INVENTORY_UNVERIFIED"


def test_failed_last_court_prevents_returning_partial_day(monkeypatch):
    def respond(request):
        if request.url.params.get("rs_idx")=="11":
            return httpx.Response(503)
        return httpx.Response(200,text=html(court=request.url.params.get("rs_idx","6")),headers={"content-type":"text/html"})
    with collector(monkeypatch, respond) as adapter:
        with pytest.raises(CollectionError) as error:
            adapter.collect_slots(FACILITY,DAY)
        assert error.value.code=="HTTP_ERROR"


def test_wrong_selected_court_and_rate_limit_stop_collection(monkeypatch):
    with collector(monkeypatch, lambda _:httpx.Response(200,text=html(court="7"),headers={"content-type":"text/html"})) as adapter:
        with pytest.raises(CollectionError) as error:
            adapter.discover_courts()
        assert error.value.code=="PAGE_SELECTION_MISMATCH"
    with collector(monkeypatch, lambda _:httpx.Response(429)) as adapter:
        with pytest.raises(CollectionError) as error:
            adapter.discover_courts()
        assert error.value.code=="ACCESS_RESTRICTED"


def test_successful_inventory_registration_is_idempotent_and_discoverable_by_plan(monkeypatch):
    from sqlalchemy import create_engine, select, func
    from sqlalchemy.orm import sessionmaker
    from tenniswatch.database import Base
    from tenniswatch.gyeyang import register_gyeyang
    from tenniswatch.models import Court, Facility, TelegramUser, Subscription
    from tenniswatch.collection import build_collection_plan
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with collector(monkeypatch, lambda _:httpx.Response(200,text=html(),headers={"content-type":"text/html"})) as adapter:
        courts=adapter.discover_courts()
    register_gyeyang(factory,courts)
    register_gyeyang(factory,courts)
    with factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(Court))==6
        facility=session.scalar(select(Facility))
        user=TelegramUser(chat_id="123",enabled=True)
        session.add(user)
        session.flush()
        session.add(Subscription(user_id=user.id,facility_id=facility.id,enabled=True))
    with factory() as session:
        targets=build_collection_plan(session,0,now=OBSERVED)
        assert len(targets)==1 and targets[0].site_key=="gyeyang_incheon"
        assert targets[0].facility.external_id==FACILITY_KEY


def test_robots_failure_prevents_calendar_request_and_no_exception_detail_is_exposed(monkeypatch):
    def respond(_):
        raise AssertionError("예약 달력 요청 금지")
    with collector(monkeypatch,respond) as adapter:
        adapter.guard.check.side_effect=CollectionError("ROBOTS_DISALLOWED","robots 거부")
        with pytest.raises(CollectionError) as error:
            adapter.discover_courts()
        assert error.value.code=="ROBOTS_DISALLOWED"


def test_both_indoor_and_outdoor_categories_are_discovered(monkeypatch):
    def respond(request):
        category=request.url.params.get("rca_id2","1010")
        source=html(category=category)
        source=source.replace(f'<option selected value="{category}">테니스</option>',
            '<option value="1010" '+('selected' if category=='1010' else '')+'>실내</option>'
            '<option value="1020" '+('selected' if category=='1020' else '')+'>실외</option>')
        soup=BeautifulSoup(source,"html.parser")
        for option in soup.select('select[name="rs_idx"] option'):
            if (int(option['value'])<9)!=(category=='1010'):
                option.decompose()
        return httpx.Response(200,text=str(soup),headers={"content-type":"text/html"})
    with collector(monkeypatch,respond) as adapter:
        courts=adapter.discover_courts()
        assert len(courts)==6 and {c.category_id for c in courts}=={"1010","1020"}


def test_single_select_without_selected_uses_actual_first_option(monkeypatch):
    source=html().replace(" selected","")
    with collector(monkeypatch,lambda _:httpx.Response(200,text=source,headers={"content-type":"text/html"})) as adapter:
        assert len(adapter.discover_courts())==6


def test_missing_selected_does_not_fall_back_to_requested_court(monkeypatch):
    source=html().replace(" selected","").replace('<select name="rs_idx">','<select name="rs_idx"><option value="0">선택하세요</option>')
    with collector(monkeypatch,lambda _:httpx.Response(200,text=source,headers={"content-type":"text/html"})) as adapter:
        with pytest.raises(CollectionError) as error:
            adapter.discover_courts()
        assert error.value.code=="PAGE_SELECTION_MISMATCH"
        assert error.value.diagnostics['rs_idx']['selected_numeric_value']=='0'
        assert error.value.diagnostics['rs_idx']['requested_numeric_value']=='6'


def test_selection_diagnostics_exclude_arbitrary_values_and_option_text(monkeypatch):
    source=html(court="7").replace('value="7"','value="private-text"').replace('실내 시험7','private-label')
    with collector(monkeypatch,lambda _:httpx.Response(200,text=source,headers={"content-type":"text/html"})) as adapter:
        with pytest.raises(CollectionError) as error:
            adapter.discover_courts()
        assert error.value.diagnostics['rs_idx']['selected_numeric_value'] is None
        assert 'private' not in str(error.value.diagnostics)


def test_dynamic_empty_selects_switch_to_browser_once_then_reuse_it(monkeypatch):
    source=html().replace('<option selected value="1010">테니스</option>', '')
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,text=source,headers={"content-type":"text/html"})
    with collector(monkeypatch,respond) as adapter:
        rendered=(BeautifulSoup(html(),"html.parser"),OBSERVED)
        adapter._get_browser=Mock(return_value=rendered)
        assert adapter._get({"rca_id":"10","rca_id2":"1010","rs_idx":"6"})==rendered
        assert adapter._get({"rca_id":"10","rca_id2":"1010","rs_idx":"6"})==rendered
        assert len(requests)==1 and adapter._get_browser.call_count==2


def test_live_dom_value_overrides_stale_html_selected_attribute(monkeypatch):
    with collector(monkeypatch,lambda _:None) as adapter:
        page=Mock()
        page.goto.return_value=Mock(ok=True,status=200)
        page.url=BOOKING_URL
        page.content.return_value=html(court="7")
        values={"rca_id":"10","rca_id2":"1010","rs_idx":"6"}
        page.content.return_value=html(court="7")
        page.locator.side_effect=lambda expression:Mock(input_value=lambda: next(
            value for name,value in values.items() if f'name="{name}"' in expression))
        adapter._page=page
        soup,observed=adapter._get_browser(values)
        from tenniswatch.collectors.gyeyang import selected_value
        assert selected_value(soup,"rs_idx")=="6"
        assert observed.tzinfo is not None
        adapter.guard.check.assert_called_once()
        page.wait_for_function.assert_called_once()


def test_browser_still_rejects_wrong_actual_court(monkeypatch):
    with collector(monkeypatch,lambda _:None) as adapter:
        page=Mock()
        page.goto.return_value=Mock(ok=True,status=200)
        page.url=BOOKING_URL
        page.content.return_value=html(court="7")
        values={"rca_id":"10","rca_id2":"1010","rs_idx":"7"}
        page.content.return_value=html(court="7")
        page.locator.side_effect=lambda expression:Mock(input_value=lambda: next(
            value for name,value in values.items() if f'name="{name}"' in expression))
        adapter._page=page
        with pytest.raises(CollectionError) as error:
            adapter._get_browser({"rca_id":"10","rca_id2":"1010","rs_idx":"6"})
        assert error.value.code=="PAGE_SELECTION_MISMATCH"


def test_browser_timeout_is_classified_and_browser_is_closed(monkeypatch):
    from playwright.sync_api import TimeoutError as BrowserTimeout
    adapter=collector(monkeypatch,lambda _:None)
    page=Mock()
    page.goto.return_value=Mock(ok=True,status=200)
    page.url=BOOKING_URL
    page.content.return_value=html()
    page.wait_for_function.side_effect=BrowserTimeout("untrusted page exception")
    adapter._page=page
    with adapter:
        with pytest.raises(CollectionError) as error:
            adapter._get_browser({"rca_id":"10","rca_id2":"1010","rs_idx":"6"})
        assert error.value.code=="PAGE_SELECTION_TIMEOUT"
        assert "untrusted" not in str(error.value)
    page.close.assert_called_once()


def test_browser_refuses_access_restrictions_without_waiting(monkeypatch):
    with collector(monkeypatch,lambda _:None) as adapter:
        page=Mock()
        page.goto.return_value=Mock(ok=False,status=403)
        adapter._page=page
        with pytest.raises(CollectionError) as error:
            adapter._get_browser({"rca_id":"10","rca_id2":"1010","rs_idx":"6"})
        assert error.value.code=="ACCESS_RESTRICTED"
        page.wait_for_function.assert_not_called()


def test_browser_reflection_failure_preserves_safe_diagnostics(monkeypatch):
    with collector(monkeypatch,lambda _:None) as adapter:
        page=Mock()
        page.goto.return_value=Mock(ok=True,status=200)
        page.url=BOOKING_URL
        # JS 확인 이후에도 코트가 바뀌었거나 DOM 구조가 다르면 성공으로 처리하지 않는다.
        page.content.return_value=html()
        values={"rca_id":"10","rca_id2":"1010","rs_idx":"99"}
        page.locator.side_effect=lambda expression:Mock(input_value=lambda: next(
            value for name,value in values.items() if f'name="{name}"' in expression))
        adapter._page=page
        with pytest.raises(CollectionError) as error:
            adapter._get_browser({"rca_id":"10","rca_id2":"1010","rs_idx":"6"})
        assert error.value.code=="PAGE_SELECTION_MISMATCH"
        assert error.value.diagnostics["stage"]=="reflect_live_values"
        assert error.value.diagnostics["rs_idx"]["live_numeric_value"]=="99"
        assert error.value.diagnostics["rs_idx"]["live_value_match_count"]==0
        page.evaluate.assert_not_called()


def test_browser_ambiguous_duplicate_values_are_reported(monkeypatch):
    with collector(monkeypatch,lambda _:None) as adapter:
        page=Mock()
        page.goto.return_value=Mock(ok=True,status=200)
        page.url=BOOKING_URL
        source=html().replace('<select name="rs_idx">','<select name="rs_idx"><option value="6">다른 시험코트</option>')
        page.content.return_value=source
        values={"rca_id":"10","rca_id2":"1010","rs_idx":"6"}
        page.locator.side_effect=lambda expression:Mock(input_value=lambda: next(
            value for name,value in values.items() if f'name="{name}"' in expression))
        adapter._page=page
        with pytest.raises(CollectionError) as error:
            adapter._get_browser({"rca_id":"10","rca_id2":"1010","rs_idx":"6"})
        assert error.value.diagnostics["rs_idx"]["live_value_match_count"]==2


def test_unexpected_browser_exception_reports_stage_without_message(monkeypatch):
    with collector(monkeypatch,lambda _:None) as adapter:
        page=Mock()
        page.goto.return_value=Mock(ok=True,status=200)
        page.url=BOOKING_URL
        page.locator.side_effect=TypeError('private-token-or-page-content')
        adapter._page=page
        with pytest.raises(CollectionError) as error:
            adapter._get_browser({"rca_id":"10","rca_id2":"1010","rs_idx":"6"})
        assert error.value.code=="BROWSER_PROCESSING_FAILED"
        assert error.value.diagnostics['stage']=='snapshot'
        assert error.value.diagnostics['exception_type']=='TypeError'
        assert 'private' not in str(error.value.diagnostics)


def test_cli_unexpected_error_always_has_safe_details(monkeypatch,capsys):
    from tenniswatch.cli import main
    import json
    monkeypatch.setattr('sys.argv',['tenniswatch','probe-gyeyang','--date','2026-10-28'])
    monkeypatch.setattr(GyeyangCollector,'collect_slots',Mock(side_effect=KeyError('private-value')))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code==1
    result=json.loads(capsys.readouterr().out)
    assert result['failure_details']['exception_type']=='KeyError'
    assert 'private' not in json.dumps(result)


def test_failure_details_never_expose_custom_exception_names():
    from tenniswatch.collectors.base import safe_failure_details
    error_class=type('PrivateSecretInExceptionName',(Exception,),{})
    assert safe_failure_details(error_class('private-message'))=={'exception_type':'OtherError'}


def test_snapshot_detects_ajax_selection_change_between_reads():
    page=Mock()
    page.content.return_value=html()
    controls={name:Mock() for name in ('rca_id','rca_id2','rs_idx')}
    controls['rca_id'].input_value.return_value='10'
    controls['rca_id2'].input_value.return_value='1010'
    controls['rs_idx'].input_value.side_effect=['6','7']
    page.locator.side_effect=lambda expression:next(control for name,control in controls.items() if f'name="{name}"' in expression)
    with pytest.raises(CollectionError) as error:
        GyeyangCollector._read_snapshot(page)
    assert error.value.code=='PAGE_SELECTION_CHANGED'
    page.evaluate.assert_not_called()


def test_snapshot_uses_native_api_even_when_page_javascript_evaluation_fails():
    page=Mock()
    page.content.return_value=html()
    values={'rca_id':'10','rca_id2':'1010','rs_idx':'6'}
    page.locator.side_effect=lambda expression:Mock(input_value=lambda:next(value for name,value in values.items() if f'name="{name}"' in expression))
    page.evaluate.side_effect=RuntimeError('page main-world script unavailable')
    snapshot=GyeyangCollector._read_snapshot(page)
    assert snapshot['values']==values
    assert snapshot['html']==html()
    page.evaluate.assert_not_called()


def category_page():
    page=Mock()
    state={'category':'1010','court':'6','values':('6','7','8')}
    category=Mock()
    category.input_value.side_effect=lambda:state['category']
    def switch(value):
        state.update(category=value,court='',values=('9','10','11'))
    category.select_option.side_effect=switch
    court=Mock()
    court.input_value.side_effect=lambda:state['court']
    court.select_option.side_effect=lambda value:state.update(court=value)
    options=Mock()
    options.count.side_effect=lambda:len(state['values'])
    options.nth.side_effect=lambda i:Mock(get_attribute=lambda _:state['values'][i])
    court.locator.return_value=options
    def locate(expression):
        if 'rca_id2' in expression:
            return category
        if 'rs_idx' in expression:
            return court
        return Mock(input_value=lambda:'10')
    page.locator.side_effect=locate
    page.url=BOOKING_URL
    page.content.side_effect=lambda:html(court=state['court'],category=state['category'])
    return page,state,category,court


def test_category_switch_discovers_court_value_without_guessing_url(monkeypatch):
    with collector(monkeypatch,lambda _:None) as adapter:
        page,state,category,court=category_page()
        adapter._page=page
        soup,_=adapter._switch_category({'rca_id':'10','rca_id2':'1020'})
        from tenniswatch.collectors.gyeyang import selected_value
        assert selected_value(soup,'rca_id2')=='1020'
        assert selected_value(soup,'rs_idx')=='9'
        category.select_option.assert_called_once_with('1020')
        court.select_option.assert_called_once_with('9')
        page.goto.assert_not_called()
        assert adapter.guard.check.call_count==2


def test_category_switch_does_not_reuse_stale_previous_courts(monkeypatch):
    with collector(monkeypatch,lambda _:None) as adapter:
        page,state,category,court=category_page()
        category.select_option.side_effect=lambda value:state.update(category=value)
        adapter._page=page
        adapter.timeout_ms=0
        with pytest.raises(CollectionError) as error:
            adapter._switch_category({'rca_id':'10','rca_id2':'1020'})
        assert error.value.code=='CATEGORY_OPTIONS_TIMEOUT'
        assert error.value.diagnostics['stage']=='switch_category'
        court.select_option.assert_not_called()


def test_category_switch_does_not_override_automatically_selected_real_court(monkeypatch):
    with collector(monkeypatch,lambda _:None) as adapter:
        page,state,category,court=category_page()
        category.select_option.side_effect=lambda value:state.update(category=value,court='10',values=('9','10','11'))
        adapter._page=page
        soup,_=adapter._switch_category({'rca_id':'10','rca_id2':'1020'})
        court.select_option.assert_not_called()
        from tenniswatch.collectors.gyeyang import selected_value
        assert selected_value(soup,'rs_idx')=='10'
