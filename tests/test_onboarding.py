from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tenniswatch.cli import seed_gangseo
from tenniswatch.collection import build_collection_plan
from tenniswatch.database import Base
from tenniswatch.models import TelegramUser, UserWatch, UserWatchFacility
from tenniswatch.onboarding import linked_identity, link_telegram, subscribe_gangseo

NOW = datetime(2026, 10, 25, 0, tzinfo=timezone.utc)


def test_link_accepts_only_private_message_with_matching_nonce_and_real_sender():
    valid = {"message": {"text": "/link known-nonce", "chat": {"id": 123, "type": "private"},
                         "from": {"id": 123, "is_bot": False}}}
    assert linked_identity([valid], "different-nonce") is None
    assert linked_identity([valid], "known-nonce") == (123, 123)
    valid["message"]["chat"]["type"] = "group"
    assert linked_identity([valid], "known-nonce") is None


def test_link_reads_only_matching_message_and_does_not_expose_token_or_chat_id():
    messages = []
    def respond(request):
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "TennisExampleBot"}})
        if request.url.path.endswith("getWebhookInfo"):
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        assert request.url.params["allowed_updates"] == '["message"]'
        return httpx.Response(200, json={"ok": True, "result": [{"message": {
            "text": "/link known-nonce", "chat": {"id": 123456, "type": "private"},
            "from": {"id": 123456, "is_bot": False}}}]})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    assert link_telegram("fake-secret-token", "known-nonce", messages.append, client=client) == (123456, 123456)
    assert "123456" not in " ".join(messages) and "fake-secret-token" not in " ".join(messages)


def test_link_conflict_does_not_remove_existing_webhook_or_leak_token():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(409)))
    with pytest.raises(ValueError) as error:
        link_telegram("fake-secret-token", "nonce", lambda text: None, client=client)
    assert "웹훅" in str(error.value) and "fake-secret-token" not in str(error.value)


def test_existing_webhook_is_detected_before_prompt_or_polling():
    methods = []
    def respond(request):
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "TennisExampleBot"}})
        return httpx.Response(200, json={"ok": True, "result": {"url": "https://worker.example.test/telegram/webhook"}})
    messages = []
    with pytest.raises(ValueError, match="이미 웹훅"):
        link_telegram("fake-secret-token", "nonce", messages.append, client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert methods == ["getMe", "getWebhookInfo"] and messages == []


def test_link_command_variant_and_whitespace_keep_nonce_validation():
    message = {"message": {"text": " /link@TennisExampleBot   known-nonce\n", "chat": {"id": 123, "type": "private"},
        "from": {"id": 123, "is_bot": False}}}
    assert linked_identity([message], "known-nonce") == (123, 123)
    assert linked_identity([message], "old-nonce") is None


def test_link_cli_saves_before_sending_confirmation_and_keeps_secrets_private(monkeypatch, tmp_path, capsys):
    import sys
    from dotenv import dotenv_values
    from tenniswatch import cli
    env_path = tmp_path / ".env"
    env_path.write_text("DATABASE_URL=local-test-value\nTELEGRAM_BOT_TOKEN=fake-secret-token\n")
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli, "find_dotenv", lambda **kwargs: str(env_path))
    monkeypatch.setattr(cli, "link_telegram", lambda *args: (123456, 123456))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-secret-token")
    sent = []
    class Sender:
        def __init__(self, config):
            pass
        def send(self, text, chat_id):
            assert dotenv_values(env_path)["TELEGRAM_CHAT_ID"] == "123456"
            sent.append(chat_id)
        def close(self):
            pass
    monkeypatch.setattr(cli, "TelegramSender", Sender)
    monkeypatch.setattr(sys, "argv", ["tenniswatch", "link-telegram"])
    cli.main()
    assert sent == ["123456"]
    output = capsys.readouterr().out
    assert "123456" not in output and "fake-secret-token" not in output
    assert dotenv_values(env_path)["DATABASE_URL"] == "local-test-value"


def test_registration_is_idempotent_and_creates_a_real_collection_target():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    seed_gangseo(factory)
    first = subscribe_gangseo(factory, "123456", date(2026, 10, 26), "18:00", "22:00", now=NOW)
    second = subscribe_gangseo(factory, "123456", date(2026, 10, 26), "18:00", "22:00", now=NOW)
    assert first == second
    with factory() as session:
        assert len(list(session.scalars(select(UserWatch)))) == 1
        assert len(list(session.scalars(select(UserWatchFacility)))) == 1
        assert len(list(session.scalars(select(TelegramUser)))) == 1
        plan = build_collection_plan(session, now=NOW)
        assert len(plan) == 1 and plan[0].play_dates == (date(2026, 10, 26),)
    with pytest.raises(ValueError):
        subscribe_gangseo(factory, "123456", date(2026, 10, 26), "22:00", "18:00", now=NOW)
    with pytest.raises(ValueError):
        subscribe_gangseo(factory, "", date(2026, 10, 26), "18:00", "22:00", now=NOW)
    engine.dispose()
