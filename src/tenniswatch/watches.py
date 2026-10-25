"""Ownership, enablement and expiry filters shared by monitor jobs."""
from datetime import datetime
from sqlalchemy import or_, select

from .models import Facility, ReservationSite, TelegramUser, UserWatch, UserWatchFacility


def eligible_watches(session, facility_id: int, watch_type: str, now: datetime,
                     statuses=("ACTIVE",)) -> list[UserWatch]:
    return list(session.scalars(select(UserWatch)
        .join(UserWatchFacility, UserWatchFacility.user_watch_id == UserWatch.id)
        .join(TelegramUser, TelegramUser.id == UserWatch.user_id)
        .join(Facility, Facility.id == UserWatchFacility.facility_id)
        .join(ReservationSite, ReservationSite.id == Facility.site_id)
        .where(UserWatchFacility.facility_id == facility_id, UserWatch.watch_type == watch_type,
               UserWatch.enabled.is_(True), UserWatch.monitoring_status.in_(statuses),
               or_(UserWatch.expires_at.is_(None), UserWatch.expires_at > now),
               TelegramUser.enabled.is_(True), Facility.enabled.is_(True),
               Facility.collection_status == "SUPPORTED", ReservationSite.enabled.is_(True))).unique())
