"""Plan subscribed collection targets and persist complete date snapshots atomically."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import time as clock
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from .collectors.base import CollectionError, CollectorRegistry, FacilityRef
from .models import CollectionRun, Facility, ReservationSite, Subscription, TelegramUser, UserWatch, UserWatchFacility
from .services import process_observation
from .domain import criteria_includes_date
from .watches import eligible_watches


@dataclass(frozen=True)
class CollectionTarget:
    site_id: int
    site_key: str
    facility: FacilityRef
    play_dates: tuple[date, ...]


def utc(value: datetime) -> datetime:
    # SQLite tests return naive timestamps; PostgreSQL timestamptz is already aware.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def build_collection_plan(session, days_ahead: int = 7, *, now: datetime | None = None, poll_interval_minutes: int | None = None) -> list[CollectionTarget]:
    if not 0 <= days_ahead <= 31:
        raise ValueError("days_ahead must be between 0 and 31")
    if poll_interval_minutes is not None and poll_interval_minutes not in (30, 60):
        raise ValueError("poll_interval_minutes must be 30 or 60")
    now = utc(now or datetime.now(timezone.utc))
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    days = [today + timedelta(days=offset) for offset in range(days_ahead + 1)]
    by_facility: dict[int, tuple[Facility, ReservationSite, set[date]]] = {}

    def include(facility, site, matching_days):
        entry = by_facility.setdefault(facility.id, (facility, site, set()))
        entry[2].update(matching_days)

    legacy = session.execute(select(Subscription, Facility, ReservationSite)
        .join(Facility, Facility.id == Subscription.facility_id)
        .join(ReservationSite, ReservationSite.id == Facility.site_id)
        .join(TelegramUser, TelegramUser.id == Subscription.user_id)
        .where(Subscription.enabled.is_(True), TelegramUser.enabled.is_(True),
               Facility.enabled.is_(True), Facility.collection_status == "SUPPORTED",
               ReservationSite.enabled.is_(True))).all()
    for sub, facility, site in legacy:
        try:
            weekdays = [int(item.strip()) for item in sub.weekdays.split(",")] if sub.weekdays else None
        except ValueError:
            continue
        include(facility, site, [day for day in days if criteria_includes_date({"weekdays": weekdays}, day)])

    watches = session.execute(select(UserWatch, Facility, ReservationSite)
        .join(UserWatchFacility, UserWatchFacility.user_watch_id == UserWatch.id)
        .join(Facility, Facility.id == UserWatchFacility.facility_id)
        .join(ReservationSite, ReservationSite.id == Facility.site_id)
        .join(TelegramUser, TelegramUser.id == UserWatch.user_id)
        .where(UserWatch.enabled.is_(True), UserWatch.watch_type == "availability",
               UserWatch.monitoring_status.in_(["PENDING_ACTIVATION", "ACTIVE"]),
               or_(UserWatch.expires_at.is_(None), UserWatch.expires_at > now),
               TelegramUser.enabled.is_(True), Facility.enabled.is_(True),
               Facility.collection_status == "SUPPORTED", ReservationSite.enabled.is_(True))).all()
    for watch, facility, site in watches:
        if isinstance(watch.criteria, dict):
            include(facility, site, [day for day in days if criteria_includes_date(watch.criteria, day)])

    latest_by_site: dict[int, datetime] = {}
    if by_facility:
        site_ids = {entry[1].id for entry in by_facility.values()}
        for run in session.scalars(select(CollectionRun).where(CollectionRun.site_id.in_(site_ids))
                                   .order_by(CollectionRun.started_at.desc())):
            latest_by_site.setdefault(run.site_id, utc(run.started_at))
    targets = []
    for facility, site, selected_days in by_facility.values():
        latest = latest_by_site.get(site.id)
        if latest and now < latest + timedelta(minutes=poll_interval_minutes or max(30, site.poll_minutes)):
            continue
        if selected_days:
            targets.append(CollectionTarget(site.id, site.key, FacilityRef(
                facility.id, facility.name, facility.external_id, facility.booking_url), tuple(sorted(selected_days))))
    return sorted(targets, key=lambda target: (target.site_id, target.facility.id))


def collect_targets(factory, registry: CollectorRegistry, days_ahead: int = 7, *, now: datetime | None = None, activate_watches: bool = False,
                    poll_interval_minutes: int | None = None) -> tuple[int, int, int]:
    with factory() as session:
        targets = build_collection_plan(session, days_ahead, now=now, poll_interval_minutes=poll_interval_minutes)
    if not targets:
        return 0, 0, 0
    runs = total_records = errors = 0
    site_keys = list(dict.fromkeys(target.site_key for target in targets))
    for site_key in site_keys:
        site_targets = [target for target in targets if target.site_key == site_key]
        try:
            collector = registry.get(site_key)
        except LookupError:
            collector = None
        try:
            for target in site_targets:
                started = clock.monotonic()
                with factory.begin() as session:
                    run = CollectionRun(site_id=target.site_id, facility_id=target.facility.id,
                        requested_dates=[day.isoformat() for day in target.play_dates], status="RUNNING",
                        started_at=utc(now or datetime.now(timezone.utc)))
                    session.add(run)
                    session.flush()
                    run_id = run.id
                records = successful_dates = 0
                error_code = None
                failed_date = None
                for day in target.play_dates:
                    try:
                        if collector is None:
                            raise CollectionError("UNSUPPORTED_COLLECTOR", "No adapter is registered for this site.")
                        observations = list(collector.collect_slots(target.facility, day))
                        if not observations:
                            raise CollectionError("NO_SLOT_DATA", "No slot observations were returned.")
                        keys = [obs.slot_key for obs in observations]
                        if len(keys) != len(set(keys)) or any(
                            obs.facility_id != target.facility.id or obs.play_date != day for obs in observations
                        ):
                            raise CollectionError("INVALID_SNAPSHOT", "Adapter returned a mismatched or duplicate slot snapshot.")
                        with factory.begin() as session:
                            if activate_watches:
                                activation_time = utc(now or datetime.now(timezone.utc))
                                for watch in eligible_watches(session, target.facility.id, "availability", activation_time,
                                                              ("PENDING_ACTIVATION", "ACTIVE")):
                                    if criteria_includes_date(watch.criteria, day):
                                        watch.monitoring_status = "ACTIVE"
                                        watch.updated_at = activation_time
                                session.flush()
                            for observation in observations:
                                process_observation(session, observation, now=now)
                            facility = session.get(Facility, target.facility.id)
                            observed_at = max(obs.observed_at for obs in observations)
                            if facility.last_collected_at is None or observed_at > utc(facility.last_collected_at):
                                facility.last_collected_at = observed_at
                        records += len(observations)
                        successful_dates += 1
                    except Exception as exc:
                        error_code = exc.code if isinstance(exc, CollectionError) else "COLLECTION_FAILED"
                        failed_date = day.isoformat()
                        errors += 1
                        # Stop on the first site error, rather than repeat a failing request for every date.
                        break
                with factory.begin() as session:
                    run = session.get(CollectionRun, run_id)
                    run.status = ("SUCCESS" if error_code is None else ("PARTIAL" if successful_dates else "ERROR"))
                    run.finished_at = datetime.now(timezone.utc)
                    run.duration_ms = int((clock.monotonic() - started) * 1000)
                    run.records_seen = records
                    run.error_code = error_code
                    run.error = f"{failed_date}: {error_code}" if error_code else None
                runs += 1
                total_records += records
        finally:
            if collector is not None:
                collector.close()
    return runs, total_records, errors
