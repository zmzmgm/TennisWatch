"""검증된 공개 달력 점검 후 계양 시설과 실제 발견한 코트를 등록한다."""
from sqlalchemy import select

from .collectors.gyeyang import BASE_URL, BOOKING_URL, FACILITY_KEY, CourtRef, GyeyangCollector
from .models import Court, Facility, ReservationSite


def register_gyeyang(factory, courts: tuple[CourtRef, ...]) -> None:
    if (len(courts) != 6 or len({c.external_id for c in courts}) != 6
            or any(sum(c.kind == kind for c in courts) != 3 for kind in ("실내", "실외"))):
        raise ValueError("실제 코트 6면의 검증 결과가 필요합니다.")
    with factory.begin() as session:
        site = session.scalar(select(ReservationSite).where(ReservationSite.key == GyeyangCollector.site_key))
        if site is None:
            site = ReservationSite(key=GyeyangCollector.site_key, name="계양스포츠클럽",
                                   base_url=BASE_URL, enabled=True, poll_minutes=60)
            session.add(site)
            session.flush()
        facility = session.scalar(select(Facility).where(
            Facility.site_id == site.id, Facility.external_id == FACILITY_KEY))
        if facility is None:
            facility = Facility(site_id=site.id, external_id=FACILITY_KEY, name="계양테니스장",
                               booking_url=BOOKING_URL, region="인천 계양구",
                               collection_status="SUPPORTED", enabled=True)
            session.add(facility)
            session.flush()
        else:
            facility.collection_status = "SUPPORTED"
        for item in courts:
            court = session.scalar(select(Court).where(Court.facility_id == facility.id,
                                                       Court.external_id == item.external_id))
            if court is None:
                session.add(Court(facility_id=facility.id, external_id=item.external_id, name=item.name))
            else:
                court.name = item.name
