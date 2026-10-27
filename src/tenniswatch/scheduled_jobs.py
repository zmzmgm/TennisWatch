"""Dispatch-only Actions runner. Never treat a dispatch receipt as successful work."""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
import re

from sqlalchemy import update, select
from .database import make_session_factory, advisory_job_lock
from .models import SchedulerJob, CollectionRun, AvailabilitySlot, ReservationSite
from .collection import collect_targets, build_collection_plan, utc
from .collectors.registry import default_registry
from .daily_reports import enqueue_daily_reports
from .opening import check_opening_schedules
from .policy_sync import refresh_policies
from .cli import _deliver

COMMANDS = ('watch-slots', 'daily-reports', 'check-policies', 'send-outbox')


def claim(factory, key, command, run_id):
    with factory.begin() as session:
        return session.execute(update(SchedulerJob).where(
            SchedulerJob.job_key == key, SchedulerJob.command == command,
            SchedulerJob.status == 'PENDING').values(status='RUNNING',
                started_at=datetime.now(timezone.utc), run_id=run_id)).rowcount == 1


def check_freshness(factory, targets):
    now = datetime.now(timezone.utc)
    missing = stale = 0
    latest_observation = None
    with factory() as session:
        for target in targets:
            site = session.get(ReservationSite, target.site_id)
            runs = list(session.scalars(select(CollectionRun).where(
                CollectionRun.facility_id == target.facility.id).order_by(CollectionRun.started_at.desc())))
            for day in target.play_dates:
                latest = next((r for r in runs if day.isoformat() in (r.requested_dates or [])), None)
                slots = list(session.scalars(select(AvailabilitySlot).where(
                    AvailabilitySlot.facility_id == target.facility.id, AvailabilitySlot.play_date == day)))
                if not latest or latest.status != 'SUCCESS' or not slots:
                    missing += 1
                    continue
                times = [utc(slot.observed_at) for slot in slots if slot.observed_at]
                if len(times) != len(slots) or any(not -timedelta(minutes=1) <= now-t <=
                    timedelta(minutes=max(30, site.poll_minutes)*2) for t in times):
                    stale += 1
                if times:
                    value = max(times)
                    latest_observation = max(latest_observation, value) if latest_observation else value
    return {'target_count': len(targets), 'unverified_dates': missing, 'stale_dates': stale,
        'freshness_checked_at': now.isoformat(),
        'last_observed_at': latest_observation.isoformat() if latest_observation else None}


def execute(factory, command):
    summary = {'runs': 0, 'records': 0, 'errors': 0}
    if command in ('watch-slots', 'daily-reports'):
        days = 6 if command == 'daily-reports' else int(os.getenv('WATCH_DAYS_AHEAD', '7'))
        with factory() as session:
            targets = build_collection_plan(session, days, report_only=command == 'daily-reports', ignore_cooldown=True)
        runs, records, errors = collect_targets(factory, default_registry(), days,
            activate_watches=True, poll_interval_minutes=30, report_only=command == 'daily-reports')
        summary.update(runs=runs, records=records, errors=errors)
        summary.update(check_freshness(factory, targets))
        summary['errors'] += summary['unverified_dates'] + summary['stale_dates']
        if command == 'daily-reports':
            # Even partial failures produce a report with explicit unknown/stale sections.
            summary['queued'] = enqueue_daily_reports(factory)
    elif command == 'check-policies':
        summary.update(refresh_policies(factory))
        summary.update(check_opening_schedules(factory, activate_watches=True))
        summary['errors'] = summary.get('policy_errors', 0) + summary.get('review_required', 0)
    sent, failed = _deliver(factory)
    summary.update(sent=sent, failed=failed)
    summary['errors'] += failed
    return summary


def run(factory, key, command, run_id):
    if not claim(factory, key, command, run_id):
        with factory() as session:
            row = session.get(SchedulerJob, key)
            if row and row.command == command and row.status in ('RUNNING', 'SUCCESS'):
                print(json.dumps({'status': 'DUPLICATE', 'original_status': row.status}))
                return 0
        print(json.dumps({'status': 'REJECTED', 'error_code': 'INVALID_OR_FAILED_RECEIPT'}))
        return 1
    summary = None
    error = None
    try:
        with factory() as connection:
            with advisory_job_lock(connection, 0x54454E4E495357) as acquired:
                if not acquired:
                    error = 'JOB_LOCK_BUSY'
                else:
                    summary = execute(factory, command)
                    if summary['errors']:
                        error = 'COLLECTION_OR_DELIVERY_FAILED'
    except Exception:
        error = 'JOB_FAILED'  # Never persist exception text containing credentials/URLs.
    with factory.begin() as session:
        session.execute(update(SchedulerJob).where(SchedulerJob.job_key == key).values(
            status='FAILED' if error else 'SUCCESS', finished_at=datetime.now(timezone.utc),
            summary=summary, error_code=error))
    print(json.dumps({'status': 'FAILED' if error else 'SUCCESS', 'summary': summary, 'error_code': error}))
    return int(error is not None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=COMMANDS)
    parser.add_argument('--job-key', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9:._-]{1,160}', args.job_key):
        parser.error('invalid job key')
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        parser.error('TELEGRAM_BOT_TOKEN is required')
    factory = make_session_factory()
    # Manual dispatch is also idempotent within a GitHub run, including reruns.
    if args.job_key.startswith('manual:'):
        from sqlalchemy.dialects.postgresql import insert
        with factory.begin() as session:
            session.execute(insert(SchedulerJob).values(job_key=args.job_key, command=args.command)
                .on_conflict_do_nothing(index_elements=['job_key']))
    raise SystemExit(run(factory, args.job_key, args.command, os.getenv('GITHUB_RUN_ID', 'local')))


if __name__ == '__main__':
    main()
