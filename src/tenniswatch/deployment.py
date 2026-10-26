"""Deployment preparation and explicit webhook registration without printing secrets."""
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key
import httpx

WORKER_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY", "LLM_API_KEY", "LLM_MODEL", "ALLOWED_TELEGRAM_CHAT_IDS")


def configuration(env_path):
    values = dict(dotenv_values(env_path))
    for key in (*WORKER_KEYS, "TELEGRAM_CHAT_ID", "DATABASE_URL", "LLM_API_URL"):
        if os.getenv(key):
            values[key] = os.environ[key]
    return values


def prepare_worker(env_path, worker_directory):
    env_path, worker_directory = Path(env_path), Path(worker_directory)
    values = configuration(env_path)
    if not values.get("TELEGRAM_WEBHOOK_SECRET"):
        values["TELEGRAM_WEBHOOK_SECRET"] = secrets.token_urlsafe(32)
        set_key(str(env_path), "TELEGRAM_WEBHOOK_SECRET", values["TELEGRAM_WEBHOOK_SECRET"])
    if not values.get("ALLOWED_TELEGRAM_CHAT_IDS") and values.get("TELEGRAM_CHAT_ID"):
        values["ALLOWED_TELEGRAM_CHAT_IDS"] = values["TELEGRAM_CHAT_ID"]
        set_key(str(env_path), "ALLOWED_TELEGRAM_CHAT_IDS", values["ALLOWED_TELEGRAM_CHAT_IDS"])
    env_path.chmod(0o600)
    missing = [key for key in WORKER_KEYS if not values.get(key)]
    if missing:
        return {"ready": False, "missing": missing}
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", values["TELEGRAM_WEBHOOK_SECRET"]):
        raise ValueError("TELEGRAM_WEBHOOK_SECRET 형식을 확인하세요.")
    chats = values["ALLOWED_TELEGRAM_CHAT_IDS"].split(",")
    if any(not re.fullmatch(r"[0-9]+", value.strip()) or not 0 < int(value) <= 2**53-1 for value in chats):
        raise ValueError("허용할 개인 채팅 ID 설정을 확인하세요.")
    for key in ("SUPABASE_URL", "LLM_API_URL"):
        if not values.get(key):
            continue
        url = urlsplit(values[key])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query:
            raise ValueError(f"{key}는 비밀값이 없는 HTTPS 주소여야 합니다.")
    payload = {key: values[key] for key in WORKER_KEYS}
    if values.get("LLM_API_URL"):
        payload["LLM_API_URL"] = values["LLM_API_URL"]
    worker_directory.mkdir(parents=True, exist_ok=True)
    bulk = worker_directory / "worker.secrets.json"
    dev = worker_directory / ".dev.vars"
    for target in (bulk, dev):
        target.touch(mode=0o600, exist_ok=True)
        target.chmod(0o600)
    bulk.write_text(json.dumps(payload), encoding="utf-8")
    dev.write_text("\n".join(f"{key}={json.dumps(value)}" for key, value in payload.items())+"\n", encoding="utf-8")
    return {"ready": True, "missing": [], "files": [bulk.name, dev.name]}


def _connection_failure_code(error):
    """Classify locally; never return exception text or token-bearing URLs."""
    if isinstance(error, httpx.TimeoutException):
        return "TIMEOUT"
    if isinstance(error, httpx.ProxyError):
        return "PROXY_ERROR"
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return "DNS_ERROR"
        if isinstance(current, ssl.SSLCertVerificationError):
            return "TLS_CERTIFICATE_ERROR"
        text = str(current).lower()
        if "certificate_verify_failed" in text or "certificate verify failed" in text:
            return "TLS_CERTIFICATE_ERROR"
        if any(part in text for part in ("nodename nor servname", "name or service not known", "getaddrinfo failed")):
            return "DNS_ERROR"
        current = current.__cause__ or current.__context__
    return "CONNECTION_ERROR"


def register_webhook(values, worker_url, *, replace=False, client=None):
    token, secret = values.get("TELEGRAM_BOT_TOKEN"), values.get("TELEGRAM_WEBHOOK_SECRET")
    if not token or not secret:
        raise ValueError("봇 토큰과 웹훅 Secret 설정이 필요합니다.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", secret):
        raise ValueError("웹훅 Secret 형식을 확인하세요.")
    url = urlsplit(worker_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("배포된 Worker의 HTTPS 주소를 지정하세요.")
    base = f"https://{url.netloc}"
    if url.path not in ("", "/", "/telegram/webhook"):
        raise ValueError("Worker 기본 주소 또는 /telegram/webhook 주소를 지정하세요.")
    endpoint = base + "/telegram/webhook"
    owns_client = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False)

    stage = "Worker 상태 확인"

    def telegram(method, body=None):
        nonlocal stage
        stage = f"Telegram {method}"
        response = client.post(f"https://api.telegram.org/bot{token}/{method}", json=body or {})
        try:
            result = response.json()
        except ValueError:
            raise ValueError("Telegram 응답을 확인하지 못했습니다.") from None
        if not response.is_success or not isinstance(result, dict) or not result.get("ok"):
            raise ValueError("Telegram 웹훅 요청이 거절됐습니다.")
        return result.get("result")

    try:
        health = client.get(base + "/health")
        if health.status_code != 200:
            raise ValueError("Worker 상태 확인이 실패했습니다.")
        stage = "Worker DB 준비 확인"
        readiness = client.get(base + "/ready", headers={"X-Telegram-Bot-Api-Secret-Token": secret})
        if readiness.status_code == 401:
            raise ValueError("Worker 웹훅 Secret 불일치 [HTTP 401]. prepare-worker 후 Cloudflare Secrets를 다시 업로드하세요.")
        if readiness.status_code == 503:
            raise ValueError("Worker 설정 또는 Supabase DB 준비 확인 실패 [HTTP 503]. alembic upgrade head 적용과 Supabase URL·service_role 키를 확인하세요.")
        if readiness.status_code != 200:
            raise ValueError(f"Worker 준비 확인 실패 [HTTP {readiness.status_code}]. 배포 주소와 Cloudflare 접근 설정을 확인하세요.")
        # This ignored update verifies configuration and the shared secret without DB writes or LLM calls.
        stage = "Worker 웹훅 인증 확인"
        probe = client.post(endpoint, headers={"X-Telegram-Bot-Api-Secret-Token": secret}, json={"update_id": 0})
        if probe.status_code != 200 or probe.json().get("ignored") is not True:
            raise ValueError("Worker 설정 또는 웹훅 Secret 검증이 실패했습니다.")
        existing = telegram("getWebhookInfo")
        if not isinstance(existing, dict):
            raise ValueError("기존 웹훅 상태를 확인하지 못했습니다.")
        if existing.get("url") and existing["url"] != endpoint and not replace:
            raise ValueError("다른 주소의 웹훅이 등록되어 있습니다. 교체하려면 --replace-webhook을 명시하세요.")
        telegram("setWebhook", {"url": endpoint, "secret_token": secret,
            "allowed_updates": ["message"], "max_connections": 1, "drop_pending_updates": False})
        confirmed = telegram("getWebhookInfo")
        if not isinstance(confirmed, dict) or confirmed.get("url") != endpoint:
            raise ValueError("등록 후 웹훅 주소 확인이 실패했습니다.")
        return {"registered": True, "pending_updates": confirmed.get("pending_update_count", 0)}
    except httpx.HTTPError as error:
        code = _connection_failure_code(error)
        raise ValueError(f"{stage} 연결 실패 [{code}]. DNS·인터넷·프록시·인증서를 확인하세요.") from None
    except (TypeError, AttributeError, json.JSONDecodeError):
        raise ValueError("배포 상태 응답을 확인하지 못했습니다.") from None
    finally:
        if owns_client:
            client.close()


def check_worker_db(values, *, client=None):
    """Read-only Supabase probe. Return only allowlisted diagnostic codes."""
    base, key = values.get('SUPABASE_URL', ''), values.get('SUPABASE_SERVICE_ROLE_KEY', '')
    if not base or not key:
        return {'ready': False, 'code': 'MISSING_SUPABASE_SETTINGS'}
    url = urlsplit(base)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        return {'ready': False, 'code': 'INVALID_SUPABASE_URL'}
    owns_client = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False)
    try:
        response = client.post(base.rstrip('/') + '/rest/v1/rpc/worker_readiness',
            headers={'apikey': key, 'Authorization': f'Bearer {key}'}, json={})
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_success:
            return {'ready': body is True, 'code': 'READY' if body is True else 'DB_ROLE_CHECK_FAILED'}
        raw_code = body.get('code') if isinstance(body, dict) else None
        allowed = {'PGRST202': 'READINESS_FUNCTION_MISSING', '42501': 'DB_PERMISSION_DENIED',
            '42P01': 'DB_TABLE_MISSING', '42703': 'DB_COLUMN_MISSING', 'PGRST301': 'INVALID_API_KEY',
            'PGRST302': 'INVALID_API_KEY', 'PGRST303': 'INVALID_API_KEY'}
        code = allowed.get(raw_code) if isinstance(raw_code, str) else None
        if code is None:
            code = {401: 'INVALID_API_KEY', 403: 'API_ACCESS_DENIED', 404: 'API_ENDPOINT_NOT_FOUND'}.get(response.status_code, 'SUPABASE_API_ERROR')
        return {'ready': False, 'code': code, 'http_status': response.status_code}
    except httpx.HTTPError as error:
        return {'ready': False, 'code': _connection_failure_code(error)}
    finally:
        if owns_client:
            client.close()


def worker_status(values, *, client=None):
    """Inspect delivery state without reading message text or invoking the model."""
    from collections import Counter
    from datetime import datetime, timezone
    checked_at = datetime.now(timezone.utc)
    owns_client = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False)
    result = {}
    safe_codes = {'PROCESSING_FAILED', 'PROCESSING_EXHAUSTED', 'LLM_REQUEST_FAILED',
        'CONTEXT_LOAD_FAILED', 'AGENT_FAILED', 'CONTEXT_SAVE_FAILED', 'REPLY_STORE_FAILED',
        'TELEGRAM_SEND_FAILED', 'INBOX_COMPLETE_FAILED', 'LLM_AUTH_FAILED', 'LLM_ACCESS_DENIED',
        'LLM_MODEL_NOT_FOUND', 'LLM_QUOTA_EXCEEDED', 'LLM_RATE_LIMITED', 'LLM_INVALID_REQUEST',
        'LLM_PROVIDER_ERROR', 'LLM_NETWORK_FAILED', 'LLM_TIMEOUT'}
    try:
        base, key, token = (values.get(name, '') for name in ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'TELEGRAM_BOT_TOKEN'))
        url = urlsplit(base)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise ValueError('SUPABASE_URL 형식을 확인하세요.')
        for table, fields, time_field, states in (
            ('telegram_updates', 'status,attempts,last_error,next_attempt_at,lease_until', 'received_at', {'pending','processing','processed','failed'}),
            ('llm_usage', 'status,error_code', 'created_at', {'RESERVED','COMPLETE','FAILED'}),
        ):
            try:
                response = client.get(base.rstrip('/')+'/rest/v1/'+table,
                    headers={'apikey': key, 'Authorization': f'Bearer {key}'},
                    params={'select': fields, 'order': time_field+'.desc', 'limit': '30'})
                if not response.is_success:
                    result[table] = {'http_status': response.status_code}
                    continue
                rows = response.json()
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    result[table] = {'code': 'INVALID_RESPONSE'}
                    continue
                errors = [row.get('last_error') or row.get('error_code') for row in rows]
                result[table] = {'recent_count': len(rows),
                    'states': dict(Counter(row.get('status') if row.get('status') in states else 'OTHER' for row in rows)),
                    'errors': dict(Counter(code if isinstance(code, str) and code in safe_codes else 'OTHER' for code in errors if code)),
                    'max_attempts': max((row.get('attempts', 0) for row in rows if type(row.get('attempts', 0)) is int), default=0)}
                if table == 'telegram_updates':
                    waiting = []
                    for row in rows:
                        if row.get('status') not in ('pending', 'processing'):
                            continue
                        field = 'next_attempt_at' if row['status'] == 'pending' else 'lease_until'
                        entry = {'status': row['status'], 'attempts': row.get('attempts') if type(row.get('attempts')) is int else None}
                        try:
                            at = datetime.fromisoformat(row.get(field, '').replace('Z', '+00:00'))
                            if at.tzinfo is None:
                                raise ValueError('Timezone required')
                            entry[field] = at.astimezone(timezone.utc).isoformat()
                            entry['due'] = at <= checked_at
                            entry['seconds_until_due'] = max(0, int((at - checked_at).total_seconds()))
                        except (ValueError, TypeError, AttributeError, OverflowError):
                            entry['timing_available'] = False
                        waiting.append(entry)
                    result[table]['waiting'] = waiting
                    result[table]['checked_at'] = checked_at.isoformat()
            except httpx.HTTPError as error:
                result[table] = {'code': _connection_failure_code(error)}
            except ValueError:
                result[table] = {'code': 'INVALID_RESPONSE'}
        # Match the reservation RPC's Korea-day accounting, including failed reservations.
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        start = datetime.now(ZoneInfo('Asia/Seoul')).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            response = client.get(base.rstrip('/')+'/rest/v1/llm_usage',
                headers={'apikey': key, 'Authorization': f'Bearer {key}'},
                params={'select': 'telegram_user_id,reserved_tokens,input_tokens,output_tokens',
                    'created_at': 'gte.'+start.isoformat(), 'limit': '1000'})
            rows = response.json() if response.is_success else None
            if isinstance(rows, list) and all(isinstance(row, dict) and type(row.get('reserved_tokens')) is int for row in rows):
                tokens = sum(row['input_tokens'] + row['output_tokens']
                    if type(row.get('input_tokens')) is int and type(row.get('output_tokens')) is int
                    else row['reserved_tokens'] for row in rows)
                owner = values.get('TELEGRAM_CHAT_ID')
                result['ai_budget_today'] = {'global_calls': len(rows), 'accounted_tokens': tokens,
                    'sample_limit_reached': len(rows) == 1000,
                    'resets_at': (start+timedelta(days=1)).isoformat()}
                if owner and owner.isdecimal():
                    result['ai_budget_today']['linked_user_calls'] = sum(row.get('telegram_user_id') == int(owner) for row in rows)
            else:
                result['ai_budget_today'] = {'code': 'BUDGET_QUERY_FAILED'}
        except httpx.HTTPError as error:
            result['ai_budget_today'] = {'code': _connection_failure_code(error)}
        except ValueError:
            result['ai_budget_today'] = {'code': 'INVALID_RESPONSE'}
        try:
            response = client.post(f'https://api.telegram.org/bot{token}/getWebhookInfo', json={})
            body = response.json()
            info = body.get('result') if isinstance(body, dict) else None
            if response.is_success and isinstance(info, dict):
                pending = info.get('pending_update_count')
                description = info.get('last_error_message', '')
                status = re.search(r'\b([45][0-9]{2})\b', description) if isinstance(description, str) else None
                result['telegram'] = {'webhook_configured': bool(info.get('url')),
                    'pending_updates': pending if type(pending) is int else None,
                    'has_delivery_error': bool(info.get('last_error_message')),
                    'last_delivery_http_status': int(status.group(1)) if status else None}
            else:
                result['telegram'] = {'http_status': response.status_code}
        except httpx.HTTPError as error:
            result['telegram'] = {'code': _connection_failure_code(error)}
        except ValueError:
            result['telegram'] = {'code': 'INVALID_RESPONSE'}
        return result
    finally:
        if owns_client:
            client.close()


def check_gemini_models(values, *, client=None):
    """Read model metadata from Google; never generate content or expose credentials."""
    key = values.get('LLM_API_KEY')
    if not key:
        return {'ok': False, 'code': 'MISSING_LLM_API_KEY'}
    owns_client = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False)
    names = set()
    page_token = None
    try:
        for _ in range(3):
            params = {'pageSize': '1000'}
            if page_token:
                params['pageToken'] = page_token
            response = client.get('https://generativelanguage.googleapis.com/v1beta/models',
                headers={'x-goog-api-key': key}, params=params)
            try:
                body = response.json()
            except ValueError:
                return {'ok': False, 'code': 'INVALID_RESPONSE', 'http_status': response.status_code}
            if not response.is_success:
                code = {401: 'INVALID_API_KEY', 403: 'API_ACCESS_DENIED', 429: 'RATE_LIMITED',
                    400: 'INVALID_REQUEST'}.get(response.status_code, 'PROVIDER_ERROR')
                error = body.get('error') if isinstance(body, dict) else None
                details = error.get('details') if isinstance(error, dict) else None
                if isinstance(details, list) and any(isinstance(item, dict) and item.get('reason') == 'API_KEY_INVALID' for item in details):
                    code = 'INVALID_API_KEY'
                return {'ok': False, 'code': code, 'http_status': response.status_code}
            models = body.get('models') if isinstance(body, dict) else None
            if not isinstance(models, list):
                return {'ok': False, 'code': 'INVALID_RESPONSE'}
            for model in models:
                if not isinstance(model, dict):
                    continue
                name = model.get('name')
                if isinstance(name, str) and re.fullmatch(r'models/gemini-[A-Za-z0-9._-]{1,100}', name) and 'generateContent' in (model.get('supportedGenerationMethods') or []):
                    names.add(name.removeprefix('models/'))
            page_token = body.get('nextPageToken')
            if not isinstance(page_token, str) or not page_token:
                break
        candidates = sorted(name for name in names if not any(part in name for part in ('image', 'audio', 'tts', 'robotics')))
        return {'ok': True, 'configured_model_listed': values.get('LLM_MODEL') in names,
            'text_model_candidates': candidates[:40], 'list_truncated': bool(page_token) or len(candidates) > 40}
    except httpx.HTTPError as error:
        return {'ok': False, 'code': _connection_failure_code(error)}
    finally:
        if owns_client:
            client.close()
