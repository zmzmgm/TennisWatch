"""Telegram delivery with explicit retryable outbox states."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str | None = None


class NotificationDeliveryError(RuntimeError):
    def __init__(self, code: str, *, permanent: bool = False, retry_after: int | None = None):
        self.code = code
        self.permanent = permanent
        self.retry_after = retry_after
        super().__init__(code)


class TelegramSender:
    def __init__(self, config: TelegramConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self.client = client or httpx.Client(timeout=15)

    def send(self, text: str, chat_id: str | None = None) -> None:
        destination = chat_id or self.config.chat_id
        if not destination:
            raise NotificationDeliveryError("MISSING_DESTINATION", permanent=True)
        try:
            response = self.client.post(
                f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
                json={"chat_id": destination, "text": text, "disable_web_page_preview": True},
            )
        except httpx.HTTPError:
            # httpx exceptions include the bot-token URL. Never propagate their text.
            raise NotificationDeliveryError("TELEGRAM_TRANSPORT_ERROR") from None
        try:
            body = response.json()
        except ValueError:
            raise NotificationDeliveryError("TELEGRAM_INVALID_RESPONSE") from None
        if not isinstance(body, dict):
            raise NotificationDeliveryError("TELEGRAM_INVALID_RESPONSE")
        if not response.is_success or not body.get("ok"):
            status = body.get("error_code", response.status_code)
            delay = body.get("parameters", {}).get("retry_after") if isinstance(body.get("parameters"), dict) else None
            retry_after = min(86400, max(1, delay)) if type(delay) is int else None
            raise NotificationDeliveryError(f"TELEGRAM_{status}" if type(status) is int else "TELEGRAM_REJECTED",
                permanent=status in (400, 401, 403, 404), retry_after=retry_after)

    def close(self) -> None:
        self.client.close()


def slot_message(*, facility: str, play_date: str, time_range: str, court: str,
                 booking_url: str, observed_at: datetime) -> str:
    return (
        "🎾 테니스장 잔여석 발견!\n\n"
        f"테니스장: {facility}\n이용 날짜: {play_date}\n이용 시간: {time_range}\n"
        f"코트: {court}\n상태: 예약 가능\n\n예약 페이지: {booking_url}\n"
        f"확인 시각: {observed_at.isoformat()}\n예약 가능 여부는 변동될 수 있습니다."
    )


def opening_message(*, facility: str, play_date: str, audience: str, opens_at: datetime,
                    kind: str, booking_url: str, source_url: str, timezone_name: str,
                    play_date_end: str | None = None) -> str:
    local = opens_at.astimezone(ZoneInfo(timezone_name))
    label = {"opening_day_before": "예약 오픈 전날 안내", "opening_hour_before": "예약 오픈 1시간 전 안내",
             "opening_opened": "예약 오픈 예정 시각이 지났어요"}[kind]
    group = "지역주민 우선 예약" if audience == "resident" else "일반 예약"
    period = play_date if not play_date_end or play_date_end == play_date else f"{play_date} ~ {play_date_end}"
    return (f"🎾 {label}\n\n테니스장: {facility}\n예약 대상 이용기간: {period}\n대상: {group}\n"
            f"예약 오픈: {local:%Y-%m-%d %H:%M} ({timezone_name})\n"
            f"예약 페이지: {booking_url}\n정책 출처: {source_url}\n"
            "정책에 따른 일정 안내이며 실제 예약 가능 여부를 보장하지 않습니다.\n"
            "정기 실행 지연으로 안내가 늦을 수 있으며, 회원 자격·휴무·전용 코트 등 실제 예약 조건은 사이트에서 확인해 주세요.")
