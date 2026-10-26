import json
import stat
import httpx
import pytest
from dotenv import dotenv_values

from tenniswatch.deployment import prepare_worker, register_webhook


def test_prepare_worker_reports_only_missing_names_and_preserves_existing_secrets(tmp_path, monkeypatch):
    from tenniswatch.deployment import WORKER_KEYS
    for key in (*WORKER_KEYS, "TELEGRAM_CHAT_ID", "LLM_API_URL"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=unchanged-private-db\nTELEGRAM_BOT_TOKEN=fake-private-token\nTELEGRAM_CHAT_ID=123456\n")
    result = prepare_worker(env, tmp_path / "worker")
    assert not result["ready"] and "LLM_API_KEY" in result["missing"]
    values = dotenv_values(env)
    assert values["DATABASE_URL"] == "unchanged-private-db"
    assert values["ALLOWED_TELEGRAM_CHAT_IDS"] == "123456"
    assert len(values["TELEGRAM_WEBHOOK_SECRET"]) >= 32
    assert "fake-private-token" not in json.dumps(result)
    assert not (tmp_path / "worker/worker.secrets.json").exists()


def test_prepare_worker_creates_private_ignored_artifacts_when_configuration_is_complete(tmp_path, monkeypatch):
    from tenniswatch.deployment import WORKER_KEYS
    values = {key: "test-value" for key in WORKER_KEYS}
    values.update(TELEGRAM_WEBHOOK_SECRET="long-test-secret-123456789", SUPABASE_URL="https://db.example.test", ALLOWED_TELEGRAM_CHAT_IDS="123456")
    for key in (*WORKER_KEYS, "TELEGRAM_CHAT_ID", "LLM_API_URL"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    worker = tmp_path / "worker"
    assert prepare_worker(env, worker)["ready"]
    assert json.loads((worker / "worker.secrets.json").read_text())["ALLOWED_TELEGRAM_CHAT_IDS"] == "123456"
    assert stat.S_IMODE((worker / "worker.secrets.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("ready,other_webhook", [(True, False), (False, False), (True, True)])
def test_register_webhook_requires_readiness_and_preserves_pending_updates(ready, other_webhook):
    methods = []
    confirmations = 0
    secret = "long-test-secret-123456789"
    def respond(request):
        nonlocal confirmations
        if request.url.host == "worker.example.test":
            if request.url.path == "/health":
                return httpx.Response(200, text="ok")
            assert request.headers["X-Telegram-Bot-Api-Secret-Token"] == secret
            if request.url.path == "/ready":
                return httpx.Response(200 if ready else 503)
            return httpx.Response(200, json={"ok": True, "ignored": True})
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "setWebhook":
            body = json.loads(request.content)
            assert body["drop_pending_updates"] is False and body["secret_token"] == secret
            return httpx.Response(200, json={"ok": True, "result": True})
        confirmations += 1
        url = "https://other.example.test" if other_webhook else ("" if confirmations == 1 else "https://worker.example.test/telegram/webhook")
        return httpx.Response(200, json={"ok": True, "result": {"url": url}})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    values = {"TELEGRAM_BOT_TOKEN": "fake-private-token", "TELEGRAM_WEBHOOK_SECRET": secret}
    if ready and not other_webhook:
        assert register_webhook(values, "https://worker.example.test", client=client)["registered"]
    else:
        with pytest.raises(ValueError):
            register_webhook(values, "https://worker.example.test", client=client)
        assert "setWebhook" not in methods


@pytest.mark.parametrize('stage_path,expected', [
    ('/health', 'Worker 상태 확인'),
    ('/ready', 'Worker DB 준비 확인'),
    ('/telegram/webhook', 'Worker 웹훅 인증 확인'),
    ('/getWebhookInfo', 'Telegram getWebhookInfo'),
    ('/setWebhook', 'Telegram setWebhook'),
])
def test_registration_connection_error_names_stage_without_leaking_secrets(stage_path, expected):
    token = 'fake-private-token'
    secret = 'long-test-secret-123456789'
    methods = []
    def respond(request):
        methods.append(request.url.path)
        if request.url.path.endswith(stage_path):
            raise httpx.ConnectError(f'connection failed: {request.url} {secret}', request=request)
        if request.url.path == '/health':
            return httpx.Response(200)
        if request.url.path == '/ready':
            return httpx.Response(200)
        if request.url.path == '/telegram/webhook':
            return httpx.Response(200, json={'ignored': True})
        return httpx.Response(200, json={'ok': True, 'result': {'url': ''}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError) as caught:
            register_webhook({'TELEGRAM_BOT_TOKEN': token, 'TELEGRAM_WEBHOOK_SECRET': secret},
                             'https://worker.example.test', client=client)
    message = str(caught.value)
    assert expected in message and '[CONNECTION_ERROR]' in message
    assert token not in message and secret not in message and 'https://' not in message
    if stage_path != '/setWebhook':
        assert not any(path.endswith('/setWebhook') for path in methods)


@pytest.mark.parametrize('kind,code', [('timeout', 'TIMEOUT'), ('proxy', 'PROXY_ERROR'),
                                      ('dns', 'DNS_ERROR'), ('tls', 'TLS_CERTIFICATE_ERROR')])
def test_safe_connection_failure_classification(kind, code):
    import socket
    import ssl
    from tenniswatch.deployment import _connection_failure_code
    if kind == 'timeout':
        error = httpx.ConnectTimeout('private data')
    elif kind == 'proxy':
        error = httpx.ProxyError('private data')
    else:
        error = httpx.ConnectError('private data')
        error.__cause__ = socket.gaierror('private host') if kind == 'dns' else ssl.SSLCertVerificationError('private cert')
    assert _connection_failure_code(error) == code


@pytest.mark.parametrize('status,expected', [(401, 'Secret 불일치'), (503, 'DB 준비 확인 실패'), (403, 'Cloudflare 접근 설정')])
def test_readiness_failure_identifies_auth_database_or_access(status, expected):
    def respond(request):
        if request.url.path == '/health':
            return httpx.Response(200)
        assert request.url.path == '/ready'
        return httpx.Response(status)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match=expected) as caught:
            register_webhook({'TELEGRAM_BOT_TOKEN': 'test-token',
                'TELEGRAM_WEBHOOK_SECRET': 'test-webhook-secret-123'}, 'https://worker.example.test', client=client)
    assert f'HTTP {status}' in str(caught.value)


@pytest.mark.parametrize('status,body,expected', [
    (200, True, 'READY'), (200, False, 'DB_ROLE_CHECK_FAILED'),
    (404, {'code': 'PGRST202', 'message': 'sensitive schema'}, 'READINESS_FUNCTION_MISSING'),
    (403, {'code': '42501'}, 'DB_PERMISSION_DENIED'),
    (401, {'message': 'secret key contents'}, 'INVALID_API_KEY'),
    (400, {'code': '42703'}, 'DB_COLUMN_MISSING'),
    (500, {'code': 'private-code', 'message': 'private-data'}, 'SUPABASE_API_ERROR'),
    (500, {'code': ['unexpected']}, 'SUPABASE_API_ERROR'),
])
def test_worker_db_probe_returns_only_safe_diagnostic_codes(status, body, expected):
    from tenniswatch.deployment import check_worker_db
    def respond(request):
        assert request.url.path == '/rest/v1/rpc/worker_readiness'
        assert request.headers['Authorization'] == 'Bearer test-private-key'
        return httpx.Response(status, json=body)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = check_worker_db({'SUPABASE_URL': 'https://db.example.test',
            'SUPABASE_SERVICE_ROLE_KEY': 'test-private-key'}, client=client)
    assert result['code'] == expected
    assert result['ready'] == (expected == 'READY')
    assert all(value not in json.dumps(result) for value in ('private-data', 'sensitive schema', 'test-private-key'))


def test_worker_db_probe_sanitizes_network_errors_and_rejects_unsafe_urls():
    from tenniswatch.deployment import check_worker_db
    def respond(request):
        raise httpx.ReadTimeout('private data')
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert check_worker_db({'SUPABASE_URL': 'https://db.example.test', 'SUPABASE_SERVICE_ROLE_KEY': 'key'}, client=client)['code'] == 'TIMEOUT'
        assert check_worker_db({'SUPABASE_URL': 'https://db.example.test/rest/v1', 'SUPABASE_SERVICE_ROLE_KEY': 'key'}, client=client)['code'] == 'INVALID_SUPABASE_URL'


def test_worker_status_reads_only_operational_fields_and_sanitizes_results():
    from tenniswatch.deployment import worker_status
    def respond(request):
        if request.url.path.endswith('/getWebhookInfo'):
            return httpx.Response(200, json={'ok': True, 'result': {'url': 'https://private-worker.test',
                'pending_update_count': 2, 'last_error_message': 'private-token: 503 private-info'}})
        select = request.url.params['select']
        assert 'message_text' not in select and 'response_text' not in select and 'chat_id' not in select
        if request.url.path.endswith('/telegram_updates'):
            return httpx.Response(200, json=[{'status': 'pending', 'attempts': 2, 'last_error': 'PROCESSING_FAILED'},
                {'status': 'failed', 'attempts': 5, 'last_error': 'private-info'}])
        return httpx.Response(200, json=[{'status': 'FAILED', 'error_code': 'LLM_REQUEST_FAILED'}])
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = worker_status({'SUPABASE_URL': 'https://db.example.test',
            'SUPABASE_SERVICE_ROLE_KEY': 'private-key', 'TELEGRAM_BOT_TOKEN': 'private-token'}, client=client)
    assert result['telegram']['pending_updates'] == 2
    assert result['telegram']['last_delivery_http_status'] == 503
    assert result['telegram_updates']['states'] == {'pending': 1, 'failed': 1}
    assert result['llm_usage']['states'] == {'FAILED': 1}
    assert all(text not in json.dumps(result) for text in ('private-info', 'private-token', 'private-key', 'private-worker'))


def test_worker_status_handles_service_and_network_failures():
    from tenniswatch.deployment import worker_status
    def respond(request):
        if request.url.path.endswith('/telegram_updates'):
            return httpx.Response(403, json={'message': 'private-info'})
        raise httpx.ReadTimeout('private-info')
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = worker_status({'SUPABASE_URL': 'https://db.example.test',
            'SUPABASE_SERVICE_ROLE_KEY': 'key', 'TELEGRAM_BOT_TOKEN': 'token'}, client=client)
    assert result['telegram_updates'] == {'http_status': 403}
    assert result['llm_usage'] == {'code': 'TIMEOUT'}
    assert result['telegram'] == {'code': 'TIMEOUT'}


def test_worker_status_accounts_today_failed_reservations_and_actual_tokens_without_ids():
    from tenniswatch.deployment import worker_status
    def respond(request):
        if request.url.path.endswith('/getWebhookInfo'):
            return httpx.Response(200, json={'ok': True, 'result': {}})
        if 'reserved_tokens' in request.url.params.get('select', ''):
            assert request.url.params['created_at'].startswith('gte.')
            return httpx.Response(200, json=[
                {'telegram_user_id': 789654123, 'reserved_tokens': 10000, 'input_tokens': None, 'output_tokens': None},
                {'telegram_user_id': 789654123, 'reserved_tokens': 10000, 'input_tokens': 80, 'output_tokens': 20},
                {'telegram_user_id': 123, 'reserved_tokens': 5000, 'input_tokens': None, 'output_tokens': None}])
        return httpx.Response(200, json=[])
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = worker_status({'SUPABASE_URL': 'https://db.example.test', 'SUPABASE_SERVICE_ROLE_KEY': 'test-key',
            'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '789654123'}, client=client)
    budget = result['ai_budget_today']
    assert budget['global_calls'] == 3
    assert budget['linked_user_calls'] == 2
    assert budget['accounted_tokens'] == 15100
    assert budget['resets_at'].endswith('T00:00:00+09:00')
    assert '789654123' not in json.dumps(result)


def test_gemini_metadata_probe_never_generates_content_and_filters_untrusted_fields():
    from tenniswatch.deployment import check_gemini_models
    def respond(request):
        assert request.method == 'GET'
        assert request.url.path == '/v1beta/models'
        assert request.headers['x-goog-api-key'] == 'private-test-key'
        assert 'key' not in request.url.params
        if 'pageToken' not in request.url.params:
            return httpx.Response(200, json={'models': [{'name': 'models/gemini-test-flash',
                'supportedGenerationMethods': ['generateContent'], 'description': 'private-data'},
                {'name': 'models/gemini-image', 'supportedGenerationMethods': ['generateContent']},
                {'name': 'models/not-a-model', 'supportedGenerationMethods': ['generateContent']}], 'nextPageToken': 'next'})
        return httpx.Response(200, json={'models': [{'name': 'models/gemini-other', 'supportedGenerationMethods': ['generateContent']}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = check_gemini_models({'LLM_API_KEY': 'private-test-key', 'LLM_MODEL': 'gemini-test-flash'}, client=client)
    assert result['configured_model_listed']
    assert result['text_model_candidates'] == ['gemini-other', 'gemini-test-flash']
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('status,details,code', [(400, [{'reason': 'API_KEY_INVALID', 'metadata': 'private-data'}], 'INVALID_API_KEY'),
    (403, [], 'API_ACCESS_DENIED'), (429, [], 'RATE_LIMITED')])
def test_gemini_metadata_failure_returns_only_safe_codes(status, details, code):
    from tenniswatch.deployment import check_gemini_models
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status,
        json={'error': {'message': 'private-data', 'details': details}}))) as client:
        result = check_gemini_models({'LLM_API_KEY': 'private-key'}, client=client)
    assert result['code'] == code
    assert 'private' not in json.dumps(result)


def test_worker_status_distinguishes_scheduled_retry_from_due_work():
    from tenniswatch.deployment import worker_status
    def respond(request):
        if request.url.path.endswith('/telegram_updates'):
            return httpx.Response(200, json=[
                {'status': 'pending', 'attempts': 4, 'next_attempt_at': '2099-01-01T00:00:00Z'},
                {'status': 'pending', 'attempts': 1, 'next_attempt_at': '2000-01-01T00:00:00Z'},
                {'status': 'processing', 'attempts': 2, 'lease_until': '2099-01-01T00:00:00Z'},
                {'status': 'pending', 'next_attempt_at': 'private-invalid-value'}])
        if request.url.path.endswith('/getWebhookInfo'):
            return httpx.Response(200, json={'ok': True, 'result': {}})
        return httpx.Response(200, json=[])
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = worker_status({'SUPABASE_URL': 'https://db.example.test',
            'SUPABASE_SERVICE_ROLE_KEY': 'key', 'TELEGRAM_BOT_TOKEN': 'token'}, client=client)
    waiting = result['telegram_updates']['waiting']
    assert waiting[0]['due'] is False and waiting[0]['seconds_until_due'] > 0
    assert waiting[1]['due'] is True and waiting[1]['seconds_until_due'] == 0
    assert waiting[2]['due'] is False and 'lease_until' in waiting[2]
    assert waiting[3]['timing_available'] is False
    assert 'private-invalid-value' not in json.dumps(result)
