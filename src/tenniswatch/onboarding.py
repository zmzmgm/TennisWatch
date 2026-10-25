"""Local bot linking and explicit one-date watch registration; no booking operations."""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
import time as clock
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Text, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB

from .models import Facility, ReservationSite, TelegramUser, UserWatch, UserWatchFacility


def linked_identity(updates, nonce):
    for update in updates:
        message = update.get("message", {}) if isinstance(update, dict) else {}
        if not isinstance(message, dict):
            continue
        chat, sender = message.get("chat", {}), message.get("from", {})
        text = message.get("text")
        parts = text.split() if isinstance(text, str) else []
        if (len(parts) != 2 or not re.fullmatch(r"/link(?:@[A-Za-z0-9_]+)?", parts[0]) or parts[1] != nonce
            or not isinstance(chat, dict) or not isinstance(sender, dict) or chat.get("type") != "private"):
            continue
        if (type(chat.get("id")) is int and 0 < chat["id"] <= 2**53 - 1
            and type(sender.get("id")) is int and sender["id"] > 0 and sender.get("is_bot") is False):
            return chat["id"], sender["id"]
    return None


def link_telegram(token, nonce, emit, *, timeout_seconds=180, client=None):
    owns_client = client is None
    client = client or httpx.Client(timeout=12)

    def request(method, **params):
        try:
            response = client.get(f"https://api.telegram.org/bot{token}/{method}", params=params)
        except httpx.HTTPError:
            raise ValueError("Telegram에 연결하지 못했습니다. 네트워크를 확인하세요.") from None
        if response.status_code == 409:
            raise ValueError("웹훅 또는 다른 수신 작업과 충돌했습니다. 다른 link-telegram 실행을 종료하세요. 기존 웹훅은 변경하지 않았습니다.")
        try:
            body = response.json()
        except ValueError:
            raise ValueError("Telegram 응답을 확인하지 못했습니다.") from None
        if not response.is_success or not isinstance(body, dict) or not body.get("ok"):
            raise ValueError("Telegram 요청이 거절됐습니다. 봇 토큰과 실행 상태를 확인하세요.")
        return body.get("result")

    try:
        bot = request("getMe")
        username = bot.get("username") if isinstance(bot, dict) else None
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]+", username):
            raise ValueError("봇 계정을 확인하지 못했습니다.")
        webhook = request("getWebhookInfo")
        if not isinstance(webhook, dict):
            raise ValueError("웹훅 상태를 확인하지 못했습니다.")
        if webhook.get("url"):
            raise ValueError("이 봇은 이미 웹훅으로 메시지를 받고 있어 로컬 연결 명령과 함께 사용할 수 없습니다. 웹훅은 변경하지 않았습니다.")
        emit(f"본인 봇 주소: https://t.me/{username}\n이 터미널을 켜 둔 채 {timeout_seconds}초 안에 아래 한 줄 전체를 봇의 개인 채팅으로 보내세요:\n/link {nonce}")
        deadline = clock.monotonic() + timeout_seconds
        progress_at = clock.monotonic() + 15
        while clock.monotonic() < deadline:
            updates = request("getUpdates", timeout=max(0, min(10, int(deadline - clock.monotonic()))),
                              limit=100, allowed_updates=json.dumps(["message"]))
            if isinstance(updates, list):
                identity = linked_identity(updates, nonce)
                if identity:
                    return identity
            if clock.monotonic() >= progress_at:
                emit(f"확인 메시지 대기 중입니다. 현재 실행에 표시된 /link 문구를 그대로 보내세요. 남은 시간: {max(0, int(deadline - clock.monotonic()))}초")
                progress_at = clock.monotonic() + 15
            clock.sleep(max(0, min(2, deadline - clock.monotonic())))
        raise ValueError("확인 시간이 끝났습니다. 명령을 다시 실행한 후 새로 표시되는 /link 한 줄을 본인 봇에게 보내세요.")
    finally:
        if owns_client:
            client.close()


def subscribe_gangseo(factory, chat_id, play_date, start_text, end_text, *, now=None):
    if not isinstance(chat_id, str) or not chat_id.isdecimal() or not 0 < int(chat_id) <= 2**53 - 1:
        raise ValueError("먼저 link-telegram 명령으로 개인 채팅을 연결하세요.")
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    if not isinstance(play_date, date) or not today <= play_date <= today + timedelta(days=31):
        raise ValueError("오늘부터 31일 이내의 이용 날짜를 지정하세요.")
    if not all(isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in (start_text, end_text)):
        raise ValueError("시작·종료 시각은 HH:MM 형식으로 지정하세요.")
    if start_text >= end_text:
        raise ValueError("종료 시각은 시작 시각보다 뒤여야 합니다.")
    with factory.begin() as session:
        facility = session.scalar(select(Facility).join(ReservationSite).where(
            ReservationSite.key == "gangseo_seoul", ReservationSite.enabled.is_(True),
            Facility.external_id == "GANGSEO02:T:03:2", Facility.enabled.is_(True),
            Facility.collection_status == "SUPPORTED"))
        if facility is None:
            raise ValueError("먼저 init-gangseo 명령으로 시설을 등록하세요.")
        user = session.scalar(select(TelegramUser).where(TelegramUser.chat_id == chat_id).with_for_update())
        if user is None:
            user = TelegramUser(chat_id=chat_id, enabled=True)
            session.add(user)
            session.flush()
        else:
            user.enabled = True
        criteria = {"facility": facility.name, "play_date": play_date.isoformat(),
                    "start_time": start_text, "end_time": end_text}
        watch = next((watch for watch in session.scalars(select(UserWatch).where(
            UserWatch.user_id == user.id, UserWatch.watch_type == "availability").with_for_update())
            if watch.criteria == criteria), None)
        if watch is None:
            if session.bind.dialect.name == "postgresql":
                digest = session.scalar(select(func.md5(cast(literal(criteria, type_=JSONB), Text))))
            else:
                digest = hashlib.md5(json.dumps(criteria, sort_keys=True).encode()).hexdigest()
            watch = UserWatch(user_id=user.id, watch_type="availability", criteria=criteria,
                              criteria_hash=digest, monitoring_status="PENDING_ACTIVATION", enabled=True)
            session.add(watch)
        elif not watch.enabled:
            watch.monitoring_status = "PENDING_ACTIVATION"
        watch.enabled = True
        watch.expires_at = datetime.combine(play_date + timedelta(days=1), time(), ZoneInfo("Asia/Seoul")).astimezone(timezone.utc)
        watch.updated_at = now
        session.flush()
        if session.get(UserWatchFacility, (watch.id, facility.id)) is None:
            session.add(UserWatchFacility(user_watch_id=watch.id, facility_id=facility.id))
        return watch.id
