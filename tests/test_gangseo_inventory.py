"""강서 세 시설 어댑터의 모의 DOM 및 등록 계약. 추가 코트 번호는 합성 자료다."""
from datetime import date, datetime, timezone
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tenniswatch.cli import seed_gangseo
from tenniswatch.collectors.base import CollectionError, FacilityRef
from tenniswatch.collectors.gangseo import (FACILITY_CATALOG, GangseoCollector, GangseoCourt,
                                          config_for_id, parse_court_options)
from tenniswatch.database import Base
from tenniswatch.gangseo import register_gangseo_courts
from tenniswatch.models import Court, Facility, ReservationSite, Subscription, TelegramUser

HTML = '<table><caption>예약 시간 선택</caption><tr><td>18:00~20:00</td><td>예약가능</td></tr></table>'
PLAY = date(2026, 10, 28)


def inventory():
    rows = [GangseoCourt(f"{group}:{item['entry_place']}", item["name"], "모의 1코트")
            for group, item in FACILITY_CATALOG.items()]
    rows.extend(GangseoCourt(f"GANGSEO02:T:03:{900 + n}", FACILITY_CATALOG["GANGSEO02:T:03"]["name"],
                            f"모의 {n}코트") for n in (2, 3, 4))
    return tuple(rows)


@pytest.mark.parametrize("external_id,kind", [("GANGSEO02:T:03:2", "T"),
    ("GANGSEO03:T:02:3", "T"), ("GANGSEO05:C:02:1", "C")])
def test_official_entry_link_preserves_facility_type(external_id, kind):
    query = parse_qs(urlsplit(GangseoCollector.booking_url_for(external_id, PLAY)).query)
    assert query["facilities_type"] == [kind]
    assert query["base_date"] == ["20261028"]
    assert query["rent_type"] == ["1001"]


@pytest.mark.parametrize("external_id", ["GANGSEO05:T:02:1", "GANGSEO03:T:03:3",
    "GANGSEO99:T:02:1", "GANGSEO02:T:03:0", "GANGSEO02:T:03:2&place=3"])
def test_unlisted_group_and_malformed_ids_are_rejected(external_id):
    with pytest.raises(ValueError):
        config_for_id(external_id)


def test_options_use_discovered_ids_and_do_not_fill_physical_court_count():
    rows = [{"value": "0", "label": "장소 선택"}, {"value": "901", "label": "모의 A코트"},
            {"value": "902", "label": "모의 B코트", "disabled": True}]
    result = parse_court_options(rows, "GANGSEO03:T:02")
    assert len(result) == 1 and result[0].external_id == "GANGSEO03:T:02:901"
    assert result[0].court_name == "모의 A코트"


@pytest.mark.parametrize("rows", [[], [{"value": "1", "label": "불러오기 오류"}],
    [{"value": "1", "label": "A"}, {"value": "1", "label": "B"}],
    [{"value": "bad", "label": "A"}]])
def test_bad_inventory_never_becomes_a_supported_court(rows):
    with pytest.raises(CollectionError):
        parse_court_options(rows, "GANGSEO02:T:03")


def test_discovery_visits_only_three_official_entries_and_validates_each_context():
    collector = GangseoCollector()
    page = Mock()
    collector._navigate = Mock(return_value=page)
    collector._select_discovery_context = Mock()
    page.evaluate.side_effect = [{"selected": {"center": group.split(":")[0], "part": group.split(":")[2]},
        "options": [{"value": c.external_id.rsplit(":", 1)[1], "label": c.court_name}
                    for c in inventory() if c.external_id.rsplit(":", 1)[0] == group]}
        for group, item in FACILITY_CATALOG.items()]
    result = collector.discover_courts()
    assert len(result) == 6
    urls = [call.args[0] for call in collector._navigate.call_args_list]
    assert [parse_qs(urlsplit(url).query)["center"][0] for url in urls] == ["GANGSEO02", "GANGSEO03", "GANGSEO05"]
    assert parse_qs(urlsplit(urls[2]).query)["facilities_type"] == ["C"]


def test_discovery_rejects_wrong_facility_even_if_court_options_exist():
    collector = GangseoCollector()
    page = Mock()
    collector._navigate = Mock(return_value=page)
    collector._select_discovery_context = Mock()
    page.evaluate.return_value = {"selected": {"center": "GANGSEO03", "part": "03"},
                                  "options": [{"value": "2", "label": "모의 1코트"}]}
    with pytest.raises(CollectionError) as error:
        collector.discover_courts()
    assert error.value.code == "PAGE_SELECTION_MISMATCH"


@pytest.mark.parametrize("court", inventory())
def test_all_three_facilities_use_the_same_checked_slot_parser(court):
    config = config_for_id(court.external_id)
    page = Mock()
    values = {f"select#{field}": config[field] for field in ("center", "part", "place")}
    values['input[name="base_date"]'] = "20261028"
    page.locator.side_effect = lambda selector: Mock(input_value=lambda: values[selector])
    page.evaluate.return_value = {"selected": {key: config[key] for key in ("center", "part", "place")},
        "actual_date": "20261028", "court_name": "모의 1코트", "html": HTML}
    collector = GangseoCollector()
    collector._navigate = Mock(return_value=page)
    result = list(collector.collect_slots(FacilityRef(7, court.name, court.external_id, "unused"), PLAY))
    assert len(result) == 1 and result[0].available
    assert result[0].court_external_id == config["place"]
    assert result[0].court_name == "모의 1코트"
    assert result[0].observed_at.tzinfo is not None


def test_registration_is_idempotent_and_preserves_existing_one_court_subscription():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    seed_gangseo(factory)
    checked = datetime(2026, 10, 26, tzinfo=timezone.utc)
    with factory.begin() as session:
        old = session.scalar(select(Facility))
        old.last_collected_at = checked
        old_id = old.id
        session.add(TelegramUser(id=1, chat_id="test-chat"))
        session.flush()
        session.add(Subscription(user_id=1, facility_id=old.id, enabled=True))
    register_gangseo_courts(factory, inventory())
    register_gangseo_courts(factory, inventory())
    with factory() as session:
        assert len(list(session.scalars(select(Facility)))) == 6
        assert len(list(session.scalars(select(Court)))) == 6
        assert session.scalar(select(Subscription)).facility_id == old_id
        old = session.get(Facility, old_id)
        assert old.last_collected_at is not None and old.external_id == "GANGSEO02:T:03:2"
        assert all(f.last_collected_at is None for f in session.scalars(select(Facility).where(Facility.id != old_id)))
    engine.dispose()


def test_partial_discovery_does_not_register_facilities():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with pytest.raises(ValueError):
        register_gangseo_courts(factory, inventory()[:1])
    with factory() as session:
        assert session.scalar(select(ReservationSite)) is None
        assert session.scalar(select(Facility)) is None
    engine.dispose()


@pytest.mark.parametrize("drift", ["date", "court"])
def test_ajax_selection_drift_is_rejected_before_slot_parsing(drift):
    external_id = "GANGSEO05:C:02:1"
    config = config_for_id(external_id)
    page = Mock()
    values = {f"select#{field}": config[field] for field in ("center", "part", "place")}
    values['input[name="base_date"]'] = "20261028"
    page.locator.side_effect = lambda selector: Mock(input_value=lambda: values[selector])
    selected = {key: config[key] for key in ("center", "part", "place")}
    if drift == "court":
        selected["place"] = "901"
    page.evaluate.return_value = {"selected": selected,
        "actual_date": "20261029" if drift == "date" else "20261028",
        "court_name": "모의 1코트", "html": HTML}
    collector = GangseoCollector()
    collector._navigate = Mock(return_value=page)
    collector._parse_slots = Mock()
    with pytest.raises(CollectionError) as error:
        list(collector.collect_slots(FacilityRef(7, "모의 코트", external_id, "unused"), PLAY))
    assert error.value.code == ("PAGE_DATE_MISMATCH" if drift == "date" else "PAGE_SELECTION_MISMATCH")
    collector._parse_slots.assert_not_called()


def test_discovery_cli_does_not_register_or_claim_availability(monkeypatch, capsys):
    import json
    from tenniswatch import cli
    collector = Mock()
    collector.__enter__ = Mock(return_value=collector)
    collector.__exit__ = Mock(return_value=False)
    collector.discover_courts.return_value = inventory()
    monkeypatch.setattr(cli, "GangseoCollector", Mock(return_value=collector, booking_url_for=GangseoCollector.booking_url_for))
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr("sys.argv", ["tenniswatch", "discover-gangseo"])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["facility_count"] == 3
    assert result["registered"] is False and result["availability_checked"] is False


@pytest.mark.parametrize("count", [1, 2, 3, 5])
def test_wujang_inventory_requires_all_four_real_options(count):
    rows = [{"value": str(900 + n), "label": f"모의 {n}코트"} for n in range(count)]
    with pytest.raises(CollectionError) as error:
        parse_court_options(rows, "GANGSEO02:T:03")
    assert error.value.code == "COURT_INVENTORY_INCOMPLETE"
    assert error.value.diagnostics["found_count"] == count


def test_missing_wujang_court_prevents_any_database_registration():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with pytest.raises(CollectionError):
        register_gangseo_courts(factory, inventory()[:-1])
    with factory() as session:
        assert session.scalar(select(ReservationSite)) is None
    engine.dispose()


def test_discovery_selects_public_controls_in_order_with_request_spacing(monkeypatch):
    from tenniswatch.collectors import gangseo
    collector = GangseoCollector(robots_guard=Mock(minimum_interval_seconds=5))
    collector._last_navigation = 100
    monkeypatch.setattr(gangseo.clock, "monotonic", lambda: 100)
    sleep = Mock()
    monkeypatch.setattr(gangseo.clock, "sleep", sleep)
    page = Mock()
    config = config_for_id("GANGSEO02:T:03:2")
    collector._select_discovery_context(page, config)
    assert [c.args[0] for c in page.locator.call_args_list] == ["select#center", "select#part", "select#place"]
    assert [c.args[0] for c in page.locator.return_value.select_option.call_args_list] == ["GANGSEO02", "03", "2"]
    assert [c.args[0] for c in sleep.call_args_list] == [5, 5, 5]


@pytest.mark.parametrize("stage", ["center", "part", "place"])
def test_discovery_timeout_identifies_stage_without_page_data(stage, monkeypatch):
    from playwright.sync_api import TimeoutError
    from tenniswatch.collectors import gangseo
    monkeypatch.setattr(gangseo.clock, "sleep", lambda _: None)
    collector = GangseoCollector(robots_guard=Mock(minimum_interval_seconds=5))
    page = Mock()
    page.wait_for_function.side_effect = [None] * ["center", "part", "place"].index(stage) + [TimeoutError("private page content")]
    with pytest.raises(CollectionError) as error:
        collector._select_discovery_context(page, config_for_id("GANGSEO02:T:03:2"))
    assert error.value.diagnostics == {"facility_group": "GANGSEO02:T:03", "stage": stage + "_options"}
    assert "private" not in str(error.value)
