from datetime import datetime, timezone
import httpx
import pytest
from tenniswatch.notifications import NotificationDeliveryError, TelegramConfig, TelegramSender, slot_message


def test_message_contains_booking_warning_and_details():
    msg = slot_message(facility="강서 테스트", play_date="2026-10-24", time_range="18:00~20:00",
                      court="2번 코트", booking_url="https://example.test", observed_at=datetime(2026,10,1,tzinfo=timezone.utc))
    assert "예약 가능 여부는 변동될 수 있습니다" in msg
    assert "https://example.test" in msg
    assert "2번 코트" in msg


def test_telegram_failure_raises_for_outbox_retry():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    sender = TelegramSender(TelegramConfig("token", "chat"), client)
    with pytest.raises(NotificationDeliveryError):
        sender.send("test")
