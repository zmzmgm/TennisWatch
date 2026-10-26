"""Workflow entry point for seeding, slot collection and Telegram outbox delivery."""
from __future__ import annotations
import argparse
from datetime import date
import os
import json
import secrets
from pathlib import Path
from dotenv import find_dotenv, load_dotenv, set_key
from sqlalchemy import select

from .collectors.base import FacilityRef
from .collectors.gangseo import BASE_URL, GangseoCollector
from .collectors.gyeyang import GyeyangCollector, FACILITY_KEY as GYEYANG_KEY, BOOKING_URL as GYEYANG_URL
from .database import advisory_job_lock, make_session_factory
from .models import Court, Facility, ReservationPolicy, ReservationSite, Subscription
from .collection import build_collection_plan, collect_targets
from .opening import check_opening_schedules, track_policy_change
from .collectors.registry import default_registry
from .collectors.base import CollectionError, safe_failure_details
from .notifications import TelegramConfig, TelegramSender
from .services import dispatch_due_notifications
from .onboarding import link_telegram, subscribe_gangseo
from .daily_reports import configure_daily_report, enqueue_daily_reports
from .deployment import configuration, prepare_worker, register_webhook, check_worker_db, worker_status, check_gemini_models


GANGSEO_KEY = "gangseo_seoul"
GANGSEO_EXT_ID = "GANGSEO02:T:03:2"
GANGSEO_FACILITY_NAME = "우장산 테니스장 1코트"


def seed_gangseo(factory) -> None:
    """Idempotently add the one publicly verified facility to a new database."""
    with factory.begin() as session:
        site = session.scalar(select(ReservationSite).where(ReservationSite.key == GANGSEO_KEY))
        if site is None:
            site = ReservationSite(key=GANGSEO_KEY, name="강서구공공체육시설", base_url=BASE_URL,
                                   enabled=True, poll_minutes=60)
            session.add(site)
            session.flush()
        facility = session.scalar(select(Facility).where(
            Facility.site_id == site.id, Facility.external_id == GANGSEO_EXT_ID))
        if facility is None:
            facility = Facility(
                site_id=site.id, external_id=GANGSEO_EXT_ID, name=GANGSEO_FACILITY_NAME,
                booking_url=f"{BASE_URL}/fmcs/28?center=GANGSEO02&facilities_type=T&part=03&place=2&rent_type=1001",
                region="서울 강서구", collection_status="SUPPORTED", enabled=True,
            )
            session.add(facility)
        else:
            facility.region = facility.region or "서울 강서구"
            facility.collection_status = "SUPPORTED"
        session.flush()
        court = session.scalar(select(Court).where(Court.facility_id == facility.id, Court.external_id == "2"))
        if court is None:
            session.add(Court(facility_id=facility.id, external_id="2", name="1코트"))
    print("Added or confirmed: 우장산 테니스장 1코트. Add a Telegram user and subscription before watching.")


def _subscription_includes_date(subscription: Subscription, play_date: date) -> bool:
    if not subscription.weekdays:
        return True
    try:
        allowed = {int(day.strip()) for day in subscription.weekdays.split(",") if day.strip()}
    except ValueError:
        return False
    return play_date.isoweekday() in allowed


def _deliver(factory):
    sender = TelegramSender(TelegramConfig(os.environ["TELEGRAM_BOT_TOKEN"], os.getenv("TELEGRAM_CHAT_ID")))
    try:
        return dispatch_due_notifications(factory, sender)
    finally:
        sender.close()


def _watch(factory, days_ahead: int, poll_interval_minutes=None) -> tuple[int, int, int]:
    ready = bool(os.getenv("TELEGRAM_BOT_TOKEN"))
    runs, records, errors = collect_targets(factory, default_registry(), days_ahead,
        activate_watches=ready, poll_interval_minutes=poll_interval_minutes)
    if ready:
        sent, failed = _deliver(factory)
        print(f"알림 성공={sent}, 실패={failed}")
        errors += failed
    return runs, records, errors


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="tenniswatch")
    parser.add_argument("command", choices=["init-gyeyang", "probe-gyeyang", "init-gangseo", "check-policies", "watch-slots", "collect-slots", "probe-gangseo", "send-outbox", "collection-plan", "verify-policy", "link-telegram", "subscribe-gangseo", "prepare-worker", "register-webhook", "check-worker-db", "worker-status", "check-gemini-models", "set-daily-report", "disable-daily-report", "daily-reports"])
    parser.add_argument("--date", help="확인/구독할 날짜 (YYYY-MM-DD)")
    parser.add_argument("--report-time", help="매일 결과를 받을 한국 시각 HH:MM")
    parser.add_argument("--start-time", help="감시 시작 시각 HH:MM")
    parser.add_argument("--end-time", help="감시 종료 시각 HH:MM")
    parser.add_argument("--policy-id", type=int, help="출처를 확인한 정책의 DB ID")
    parser.add_argument("--github-output", action="store_true", help="GitHub Actions 단계 출력 기록")
    parser.add_argument("--url", help="배포한 Worker의 HTTPS 주소")
    parser.add_argument("--replace-webhook", action="store_true", help="기존 다른 웹훅 주소를 명시적으로 교체")
    args = parser.parse_args()
    if args.command in ("probe-gyeyang", "init-gyeyang"):
        try:
            play_date = date.fromisoformat(args.date or "")
        except ValueError:
            parser.error("--date YYYY-MM-DD로 확인할 날짜를 지정하세요.")
        if args.command == "init-gyeyang" and not os.getenv("DATABASE_URL"):
            parser.error("DATABASE_URL이 필요합니다.")
        try:
            with GyeyangCollector() as collector:
                observations = list(collector.collect_slots(
                    FacilityRef(1, "계양테니스장", GYEYANG_KEY, GYEYANG_URL), play_date))
                courts = collector.discover_courts()
            if args.command == "init-gyeyang":
                from .gyeyang import register_gyeyang
                register_gyeyang(make_session_factory(), courts)
            print(json.dumps({"ok": True, "registered": args.command == "init-gyeyang",
                "date": play_date.isoformat(), "court_count": len(courts),
                "slots": [{"court": obs.court_name, "starts_at": obs.starts_at.isoformat(),
                    "ends_at": obs.ends_at.isoformat(), "available": obs.available,
                    "observed_at": obs.observed_at.isoformat(), "booking_url": obs.booking_url}
                    for obs in observations]}, ensure_ascii=False))
        except Exception as exc:
            code = exc.code if isinstance(exc, CollectionError) else "COLLECTION_FAILED"
            result = {"ok": False, "error_code": code}
            result["failure_details"] = safe_failure_details(exc)
            if isinstance(exc, CollectionError) and hasattr(exc, "diagnostics"):
                result["selection_diagnostics"] = exc.diagnostics
            print(json.dumps(result, ensure_ascii=False))
            raise SystemExit(1)
        return
    if args.command in ("prepare-worker", "register-webhook", "check-worker-db", "worker-status", "check-gemini-models"):
        env_path = Path(find_dotenv(usecwd=True) or (Path.cwd() / ".env"))
        try:
            if args.command == "check-gemini-models":
                result = check_gemini_models(configuration(env_path))
                print(json.dumps(result, ensure_ascii=False))
                if not result["ok"]:
                    raise SystemExit(1)
            elif args.command == "worker-status":
                print(json.dumps(worker_status(configuration(env_path)), ensure_ascii=False))
            elif args.command == "check-worker-db":
                result = check_worker_db(configuration(env_path))
                print(json.dumps(result, ensure_ascii=False))
                if not result["ready"]:
                    raise SystemExit(1)
            elif args.command == "prepare-worker":
                result = prepare_worker(env_path, env_path.parent / "apps" / "telegram-worker")
                print(json.dumps(result, ensure_ascii=False))
                if not result["ready"]:
                    raise SystemExit(1)
            else:
                if not args.url:
                    parser.error("register-webhook --url HTTPS주소를 지정하세요.")
                print(json.dumps(register_webhook(configuration(env_path), args.url, replace=args.replace_webhook), ensure_ascii=False))
        except ValueError as exc:
            parser.error(str(exc))
        return
    if args.command == "link-telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            parser.error(".env에 TELEGRAM_BOT_TOKEN을 먼저 저장하세요.")
        try:
            chat_id, _user_id = link_telegram(token, secrets.token_urlsafe(8), lambda message: print(message, flush=True))
            env_path = Path(find_dotenv(usecwd=True) or (Path.cwd() / ".env"))
            set_key(str(env_path), "TELEGRAM_CHAT_ID", str(chat_id))
            env_path.chmod(0o600)
        except ValueError as exc:
            parser.error(str(exc))
        print("개인 채팅 연결을 확인하고 .env에 저장했습니다. 채팅 ID는 출력하지 않습니다.")
        sender = TelegramSender(TelegramConfig(token))
        try:
            sender.send("🎾 TennisWatch 개인 채팅 연결이 완료됐어요. 이제 터미널에서 감시할 날짜와 시간을 등록해 주세요.", chat_id=str(chat_id))
        except Exception:
            print("연결 정보는 저장됐지만 봇 확인 답장을 보내지 못했습니다. 연결 명령을 반복할 필요는 없습니다.")
        finally:
            sender.close()
        return
    if args.command == "probe-gangseo":
        try:
            play_date = date.fromisoformat(args.date or "")
        except ValueError:
            parser.error("probe-gangseo requires --date YYYY-MM-DD")
        facility = FacilityRef(1, GANGSEO_FACILITY_NAME, GANGSEO_EXT_ID, BASE_URL)
        try:
            with GangseoCollector() as collector:
                observations = list(collector.collect_slots(facility, play_date))
            print(json.dumps({"date": play_date.isoformat(), "slots": [
                {"court": obs.court_name, "starts_at": obs.starts_at.isoformat(),
                 "ends_at": obs.ends_at.isoformat(), "available": obs.available,
                 "observed_at": obs.observed_at.isoformat(), "booking_url": obs.booking_url}
                for obs in observations]}, ensure_ascii=False))
        except Exception as exc:
            code = exc.code if isinstance(exc, CollectionError) else "COLLECTION_FAILED"
            print(json.dumps({"ok": False, "error_code": code}, ensure_ascii=False))
            raise SystemExit(1)
        return
    if not os.getenv("DATABASE_URL"):
        parser.error("DATABASE_URL is required")
    factory = make_session_factory()
    if args.command == "init-gangseo":
        seed_gangseo(factory)
        return
    if args.command == "subscribe-gangseo":
        try:
            play_date = date.fromisoformat(args.date or "")
        except ValueError:
            parser.error("--date YYYY-MM-DD와 --start-time HH:MM --end-time HH:MM을 지정하세요.")
        if not os.getenv("TELEGRAM_CHAT_ID"):
            parser.error("먼저 tenniswatch link-telegram으로 개인 채팅을 연결하세요.")
        with factory() as lock_session:
            with advisory_job_lock(lock_session, 0x54454E4E495357) as acquired:
                if not acquired:
                    parser.error("다른 감시 작업이 실행 중입니다. 잠시 후 다시 등록하세요.")
                try:
                    seed_gangseo(factory)
                    watch_id = subscribe_gangseo(factory, os.environ["TELEGRAM_CHAT_ID"], play_date, args.start_time, args.end_time)
                except ValueError as exc:
                    parser.error(str(exc))
        print(f"감시 조건 등록 완료: 조건 번호={watch_id}, 이용일={play_date}, 시간={args.start_time}~{args.end_time}")
        print("collection-plan으로 수집 대상을 확인한 뒤 watch-slots를 실행하세요.")
        return
    try:
        days_ahead = int(os.getenv("WATCH_DAYS_AHEAD", "7"))
        interval_text = os.getenv("WATCH_INTERVAL_MINUTES", "")
        interval = int(interval_text) if interval_text else None
    except ValueError:
        parser.error("수집 범위와 간격은 정수여야 합니다.")
    if not 0 <= days_ahead <= 31 or interval not in (None, 30, 60):
        parser.error("WATCH_DAYS_AHEAD는 0~31, WATCH_INTERVAL_MINUTES는 빈 값·30·60을 사용하세요.")
    if args.command in ("set-daily-report", "disable-daily-report"):
        from .models import DailyReportSetting, TelegramUser
        import re
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not chat_id:
            parser.error("연결한 TELEGRAM_CHAT_ID 설정이 필요합니다.")
        if args.command == "set-daily-report" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", args.report_time or ""):
            parser.error("--report-time HH:MM 형식으로 한국 시각을 지정하세요.")
        with factory.begin() as session:
            user = session.scalar(select(TelegramUser).where(TelegramUser.chat_id == chat_id))
            if user is None or not user.enabled:
                parser.error("연결한 활성 사용자를 찾지 못했습니다.")
            existing = session.get(DailyReportSetting, user.id)
            if args.command == "disable-daily-report":
                if existing:
                    configure_daily_report(session,user.id,existing.minute_of_day,enabled=False)
                result = {"saved":True,"enabled":False}
            else:
                hour, minute = map(int,args.report_time.split(":"))
                setting = configure_daily_report(session,user.id,hour*60+minute)
                result = {"saved":True,"local_time":args.report_time,"timezone":"Asia/Seoul",
                    "next_run_at":setting.next_run_at.isoformat(),"scope":"existing_availability_watches"}
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command == "daily-reports":
        if not os.getenv("TELEGRAM_BOT_TOKEN"):
            parser.error("TELEGRAM_BOT_TOKEN 설정이 필요합니다.")
        queued = enqueue_daily_reports(factory)
        sent, failed = _deliver(factory)
        print(json.dumps({"queued":queued,"sent":sent,"failed":failed}))
        if failed:
            raise SystemExit(1)
        return
    if args.command == "collection-plan":
        registry = default_registry()
        with factory() as session:
            targets = build_collection_plan(session, days_ahead, poll_interval_minutes=interval)
        needs_browser = False
        for target in targets:
            try:
                needs_browser |= registry.get(target.site_key).capabilities.requires_browser
            except LookupError:
                pass
        result = {"target_count": len(targets), "has_targets": bool(targets), "needs_browser": needs_browser}
        print(json.dumps(result))
        if args.github_output:
            output = os.getenv("GITHUB_OUTPUT")
            if not output:
                parser.error("GITHUB_OUTPUT이 설정되지 않았습니다.")
            with open(output, "a", encoding="utf-8") as stream:
                stream.write(f"has_targets={str(bool(targets)).lower()}\nneeds_browser={str(needs_browser).lower()}\n")
        return
    if args.command in ("watch-slots", "check-policies", "send-outbox") and not os.getenv("TELEGRAM_BOT_TOKEN"):
        parser.error("이 명령에는 TELEGRAM_BOT_TOKEN이 필요합니다. 발송 없는 수집은 collect-slots를 사용하세요.")
    if args.command == "send-outbox":
        sent, failed = _deliver(factory)
        print(f"알림 성공={sent}, 실패={failed}")
        if failed:
            raise SystemExit(1)
        return
    with factory() as lock_session:
        with advisory_job_lock(lock_session, 0x54454E4E495357) as acquired:
            if not acquired:
                print("다른 감시 작업이 실행 중이어서 이번 실행을 건너뜁니다.")
                return
            if args.command == "verify-policy":
                if not args.policy_id:
                    parser.error("verify-policy --policy-id ID로 출처를 직접 확인한 정책을 지정하세요.")
                with factory.begin() as session:
                    policy = session.get(ReservationPolicy, args.policy_id)
                    if policy is None:
                        parser.error("정책을 찾을 수 없습니다.")
                    track_policy_change(session, policy, verify=True)
                print("정책 검증 상태를 저장했습니다.")
                return
            if args.command == "check-policies":
                result = check_opening_schedules(factory, activate_watches=True)
                sent, failed = _deliver(factory)
                print(json.dumps({**result, "sent": sent, "failed": failed}))
                if failed:
                    raise SystemExit(1)
                return
            if args.command == "collect-slots":
                runs, records, errors = collect_targets(factory, default_registry(), days_ahead,
                                                        poll_interval_minutes=interval)
            else:
                runs, records, errors = _watch(factory, days_ahead, interval)
    print(f"수집 실행={runs}, 슬롯 관측={records}, 오류={errors}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
