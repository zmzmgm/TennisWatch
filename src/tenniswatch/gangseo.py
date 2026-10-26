"""강서 공개 선택 목록에서 발견한 코트를 기존 코트별 시설 구조로 등록한다."""
from sqlalchemy import select

from .collectors.gangseo import BASE_URL, FACILITY_CATALOG, GangseoCollector, GangseoCourt, config_for_id
from .models import Court, Facility, ReservationSite


def register_gangseo_courts(factory, courts: tuple[GangseoCourt, ...]) -> None:
    groups = {item.external_id.rsplit(":", 1)[0] for item in courts}
    if (groups != set(FACILITY_CATALOG) or len({c.external_id for c in courts}) != len(courts)
        or not 3 <= len(courts) <= 60):
        raise ValueError("세 시설의 전체 선택 목록 검증 결과가 필요합니다.")
    for item in courts:
        config_for_id(item.external_id)
        group = item.external_id.rsplit(":", 1)[0]
        if item.facility_name != FACILITY_CATALOG[group]["name"] or not item.court_name.strip() or len(item.court_name) > 80:
            raise ValueError("검증되지 않은 시설 또는 코트 정보입니다.")
    with factory.begin() as session:
        site = session.scalar(select(ReservationSite).where(ReservationSite.key == GangseoCollector.site_key))
        if site is None:
            site = ReservationSite(key=GangseoCollector.site_key, name="강서구공공체육시설",
                                   base_url=BASE_URL, enabled=True, poll_minutes=60)
            session.add(site)
            session.flush()
        for item in courts:
            facility = session.scalar(select(Facility).where(
                Facility.site_id == site.id, Facility.external_id == item.external_id))
            if facility is None:
                facility = Facility(site_id=site.id, external_id=item.external_id, name=item.name,
                    booking_url=GangseoCollector.booking_url_for(item.external_id),
                    region="서울 강서구", collection_status="SUPPORTED", enabled=True)
                session.add(facility)
                session.flush()
            else:
                # 기존 우장산 1코트의 ID·구독·상태·관측값은 그대로 보존한다.
                facility.name = item.name
                facility.region = "서울 강서구"
                facility.booking_url = GangseoCollector.booking_url_for(item.external_id)
                facility.collection_status = "SUPPORTED"
            place = config_for_id(item.external_id)["place"]
            court = session.scalar(select(Court).where(Court.facility_id == facility.id, Court.external_id == place))
            if court is None:
                session.add(Court(facility_id=facility.id, external_id=place, name=item.court_name))
            else:
                court.name = item.court_name
