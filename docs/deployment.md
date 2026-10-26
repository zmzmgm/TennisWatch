# Phase 7 — 무료 서버리스 배포와 운영

## 현재 검증 범위

배포용 코드와 모의 테스트를 준비했습니다. 사용자는 Cloudflare Worker 배포·Secrets 업로드·Telegram 웹훅 등록 후 Gemini를 통한 기존 감시 조건 조회 응답을 확인했습니다. Supabase 마이그레이션은 사용자 실행에서 `0007 (head)`를 확인했습니다. GitHub Actions의 실제 정기 수집 및 신규 잔여석 알림 전체 경로는 아직 검증하지 않았습니다.

이 개발 환경의 GitHub CLI 인증은 유효하지 않고 프로젝트 `.git` 생성은 제한됩니다. 게시 준비는 비밀 파일을 제외한 별도 복사본에서 수행합니다. 사용자가 Phase 6에서 보고한 실제 수집 결과는 실행 1회, 슬롯 관측 3개, 오류 0개입니다. 이 결과만으로 예약 가능 슬롯이나 Telegram 잔여석 알림 성공을 의미하지는 않습니다.


## 1. 비밀 설정 준비

프로젝트 루트의 기존 `.env`를 편집합니다. 기존 파일을 예제 파일로 덮어쓰지 마세요.

- `DATABASE_URL`: 기존 Supabase PostgreSQL 연결 문자열 유지.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: 기존 연결 값 유지.
- `SUPABASE_URL`: Supabase 프로젝트의 HTTPS API 주소.
- `SUPABASE_SERVICE_ROLE_KEY`: Supabase 프로젝트 설정의 서버 전용 `service_role` 키. `anon`/publishable 키가 아닙니다. 브라우저에 제공하지 않습니다.
- `LLM_API_KEY`: 선택한 모델 공급자의 키.
- `LLM_MODEL`: 공급자가 지원하는 도구 호출 모델의 정확한 이름.
- `LLM_API_URL`: Chat Completions 호환 API의 **전체 경로**. 생략 시 OpenAI 주소를 사용하므로, 다른 공급자는 반드시 지정합니다. 모델·공급자·무료 사용 조건은 사용자가 선택합니다.

키·비밀번호를 대화창에 보내지 마세요. 실제 키가 노출된 적이 있으면 해당 서비스에서 교체하고 `.env`도 갱신하세요.

```bash
cd /Users/kkh/TennisWatch
.venv/bin/tenniswatch prepare-worker
```

이 명령은 웹훅 Secret을 생성하고 이미 연결한 개인 채팅을 허용 목록에 넣습니다. 설정이 부족하면 **항목 이름만** 출력합니다. 완료되면 `apps/telegram-worker/worker.secrets.json`과 `.dev.vars`를 권한 `600`으로 생성합니다. `.env`, 두 생성 파일과 빌드 결과는 Git에서 제외됩니다. 허용 목록 밖의 채팅은 DB 저장이나 AI 호출 없이 무시합니다. 허용할 개인 채팅을 추가할 때만 `ALLOWED_TELEGRAM_CHAT_IDS`를 쉼표로 구분해 수정합니다.

## 2. DB 적용과 로컬 검증

```bash
.venv/bin/alembic upgrade head
.venv/bin/alembic current
.venv/bin/pytest -q
cd apps/telegram-worker
npm ci
npm test
npm run typecheck
npm run build:check
npm audit
```

최신 DB 버전은 `0007`입니다. LLM 사용량 테이블, 예산 예약 RPC, 처리 잠금 토큰, 재전송용 답변 저장, 배포 준비 상태 점검 RPC를 추가합니다. 운영 DB 적용 전 Supabase에서 백업 가능 여부를 확인하고, 변경에 사용한 마이그레이션 파일을 보관하세요. `0007` 다운그레이드는 데이터 손실을 피하기 위해 차단되어 있습니다.

이 환경에서 확인한 결과는 Python 71개, Worker 33개 테스트 통과 및 타입 검사·배포 묶음 생성 성공입니다. PostgreSQL 마이그레이션은 SQL 생성까지만 검증했습니다. 실제 DB의 RPC 권한·동시성 검증은 적용 후 필요합니다. `npm audit`은 네트워크 제한으로 확인하지 못했습니다. 이전 설치에서 보고된 취약점은 해소됐다고 간주하지 말고, 감사 보고서에서 실제 패키지와 수정 버전을 확인하세요. `npm audit fix --force`를 일괄 실행하지 마세요.

## 3. Cloudflare 배포

Cloudflare 계정의 Workers 무료 플랜을 사용합니다. 아래 명령은 로컬 터미널에서 실행합니다.

```bash
cd /Users/kkh/TennisWatch/apps/telegram-worker
npx wrangler login
npx wrangler deploy
npx wrangler secret bulk worker.secrets.json
```

처음 배포한 직후에는 Secret이 없으므로 요청을 처리하지 않습니다. Secret 업로드 후 배포된 `https://…workers.dev` 주소를 보관합니다. `wrangler.jsonc`의 작업은 매분 inbox 한 건을 처리합니다. 웹훅은 DB 저장 후 빠르게 응답하고, 긴 AI 작업을 HTTP `waitUntil`에 의존하지 않습니다. 대화 응답은 다음 작업까지 기다릴 수 있으며, 한 건/분 처리량을 넘으면 대기열이 늘어납니다.

```bash
cd /Users/kkh/TennisWatch
.venv/bin/tenniswatch register-webhook --url https://실제주소.workers.dev
```

위 주소는 실제 배포 주소로 바꿉니다. 이 명령은 `/health`, 비밀 헤더로 보호된 `/ready`, 무시되는 시험 업데이트를 검사한 뒤 Telegram 웹훅을 등록하고 주소를 다시 확인합니다. `/ready`는 서비스 역할·DB 스키마 접근을 검사하지만 LLM API나 모든 수집 기능의 성공을 보장하지 않습니다. 기존에 다른 웹훅이 있으면 중단합니다. 의도적으로 교체할 때만 `--replace-webhook`을 추가하세요. 대기 중인 업데이트는 삭제하지 않습니다.

웹훅 등록 후 `link-telegram`처럼 Telegram `getUpdates`를 사용하는 연결 명령은 함께 실행하지 마세요.

## 4. GitHub 저장소와 정기 실행

새 저장소의 이름과 공개 여부를 정한 뒤 GitHub에 생성합니다. 공개 표준 실행기는 무료 사용에 유리하지만, **소스와 커밋 기록을 먼저 점검해야 합니다**. `.env`, 채팅 ID, 키, 실제 사용자 대화, DB 덤프를 올리지 않습니다. 아직 Git 초기화가 안 된 폴더라면:

```bash
git init -b main
git add .
git status --short
git diff --cached --stat
```

게시 목록을 검토한 뒤 커밋과 원격 연결을 진행합니다. 원격 주소는 실제 생성한 저장소 주소를 사용하세요.

```bash
git commit -m "TennisWatch AI 서버리스 배포 준비"
git remote add origin https://github.com/본인계정/TennisWatch.git
git push -u origin main
```

저장소의 Settings → Secrets and variables → Actions → Secrets에서 다음을 설정합니다.

| 이름 | 용도 |
| --- | --- |
| `DATABASE_URL` | Python 수집기 DB 연결 |
| `TELEGRAM_BOT_TOKEN` | 알림 전송 |
| `TELEGRAM_CHAT_ID` | 기존 개인 연결 호환용, 선택 |

Worker의 `SUPABASE_SERVICE_ROLE_KEY`, `LLM_API_KEY`는 GitHub 수집 작업에 필요하지 않습니다. 정기 수집에서는 AI를 호출하지 않습니다.

Actions에서 `Verification` 결과를 확인한 후 `Availability watch`를 수동 실행합니다. `collection-plan`의 대상 수와 실행·오류 결과를 확인합니다. `Reservation opening check`도 수동 실행합니다. 검증된 DB 정책이 없으면 오픈 알림 0건이 정상입니다. 실제 사이트 정책 자동 갱신은 아직 지원하지 않습니다.

기본 주기는 잔여석 매시 7분, 오픈 확인 매시 17분입니다. 30분 감시는 `watch.yml`의 cron을 `7,37 * * * *`, Actions 변수 `WATCH_INTERVAL_MINUTES`를 `30`으로 함께 바꿉니다. GitHub 예약은 지연될 수 있습니다. 기본 브랜치에 Workflow가 있어야 하며, 공개 저장소가 오래 비활성 상태이면 예약 작업이 중지될 수 있으므로 Actions 상태도 확인합니다.

## 5. 실제 통합 확인

1. 본인 봇에 “현재 감시 중인 조건 알려줘”를 보냅니다. 1분 이후 실제 DB 조건이 답변에 반영되는지 확인합니다.
2. “2026-10-26 강서구 18시 이후 찾아줘”처럼 **실제 이용하려는 날짜**로 검색합니다. 마지막 확인 시각과 오래된 데이터 안내를 확인합니다.
3. “없으면 알려줘”로 생성한 조건을 목록에서 확인하고 시간을 변경한 뒤 다시 조회합니다. 필요 없어진 시험 조건은 비활성화합니다.
4. Actions에서 잔여석 감시를 수동 실행합니다. 실제 예약 가능 슬롯이 조건에 맞을 때만 Telegram 알림이 와야 합니다. 0건이면 임의로 실제 DB 상태를 바꿔 알림을 만들어 내지 않습니다.
5. 같은 수집을 반복해 동일 이벤트 알림이 반복되지 않는지 확인합니다. 실제 신규 잔여석이 없으면 발송 경로 전체 검증은 미완료로 기록합니다.

오류는 Worker의 안전한 오류 코드, `telegram_updates`, `llm_usage`, Python 수집 실행 기록으로 확인합니다. DB 테이블과 Cloudflare 로그에는 사용자 정보가 있으므로 화면 전체를 공개하지 마세요. 다른 사용자의 감시 조건이 조회되지 않는지도 별도 테스트 계정으로 확인합니다.

## 무료 한도와 실행 제한

공식 문서 확인일: 2026-10-25. 계정 화면의 실제 한도와 요금 정책을 우선합니다.

| 서비스 | 공식 무료 한도 | 운영 기준 |
| --- | --- | --- |
| Cloudflare Workers | 일 100,000 요청, 호출당 CPU 10ms, 하위 요청 50개, 메모리 128MB | inbox 한 건/분, AI 왕복 최대 4회·회당 도구 1개. 네트워크 대기는 CPU 시간과 다르며 실제 운영 CPU는 배포 후 확인 |
| Supabase | DB 500MB, 전송 5GB, 파일 1GB, 활성 프로젝트 2개, 비활성 1주 후 일시 정지 가능 | 사용량·DB 크기를 확인. 무료 자동 백업을 가정하지 않음 |
| GitHub Actions | 공개 저장소 표준 실행기 무료. 개인 Free의 비공개 저장소 월 2,000분 | 두 시간별 Workflow는 월 약 1,440회. 각 실행 최소 1분 및 브라우저 설치 시간으로 비공개 한도 초과 가능 |
| LLM | 공급자·모델·지역·계정에 따라 다름 | 무료 API 제공 여부를 먼저 확인. 앱 제한만으로 과금 0원을 보장할 수 없음 |
| Telegram | 전송 속도 제한 적용 | 제한 시 재시도하며 브로드캐스트 유료 옵션을 사용하지 않음 |

출처: [Cloudflare 제한](https://developers.cloudflare.com/workers/platform/limits/), [Supabase 요금](https://supabase.com/pricing), [GitHub Actions 과금](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [GitHub 예약 실행](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [Telegram FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).

LLM은 기본 **사용자별 일 24회, 전체 일 100회, 예약 토큰 150,000개**로 제한합니다. 한 메시지에 여러 모델 호출이 발생하므로 메시지 수와 다릅니다. 자정은 한국 시간 기준입니다. DB에서 호출 전에 예산을 예약하고 실제 토큰을 기록합니다. 실제 사용량을 받지 못하면 예약량을 유지합니다. 예약량은 입력 JSON 바이트와 최대 출력의 보수적 추정으로 공급자 청구량과 정확히 같지 않습니다. 공급자 자체의 무료 전용 계정/청구 한도도 반드시 설정하세요.

한도를 넘으면 AI만 중단되며 별도 GitHub 수집은 계속됩니다. 작업 실패를 포함해 시도한 모델 호출은 일일 호출 수에 포함합니다. DB 사용량 기록 실패 시 추가 AI 호출을 진행하지 않습니다.

## 장애 대응과 보존

- 답변 없음: 허용 목록, Worker Secret, `0007` 적용, 매분 Cron, inbox 상태, AI 일일 한도를 차례로 확인합니다.
- 수집 오류: 이전 관측을 보존합니다. [감시 운영 문서](monitor-operations.md)의 재시도 절차를 사용하고, 사이트 접근 제한을 우회하지 않습니다.
- DB 일시 정지: Supabase에서 복구하고 `/ready`와 수동 Workflow로 연결을 확인합니다.
- CPU 한도 초과: Cloudflare 관측 지표를 확인합니다. 무료 플랜에서 안정 실행이 되기 전까지 운영 완료로 표시하지 않습니다. 유료 플랜 자동 전환은 하지 않습니다.
- 응답 재시도: 이미 저장된 답변을 재사용합니다. Telegram 전송 성공과 DB 완료 기록 사이의 중단으로 드물게 중복 전송될 수 있습니다.
- 저장 공간: inbox 대화·답변, 사용량, 수집·이벤트 이력은 계속 증가합니다. 자동 삭제는 구현하지 않았습니다. 먼저 크기와 보존 필요 기간을 확인하고 백업한 뒤 별도 보존 정책을 적용합니다. 현재 날짜 사용량과 진행 중 작업을 삭제하면 한도/중복 방지가 깨질 수 있습니다.
- 중지: GitHub 두 예약 Workflow와 Worker Cron을 중지합니다. 봇 웹훅을 해제하면 대화도 중지됩니다. 키 노출 시 관련 서비스에서 키를 교체하고 Worker Secrets/GitHub Secrets/.env를 갱신합니다.

### 웹훅 등록 중 연결 오류 구분

`register-webhook`은 실패한 단계(Worker 상태 확인, DB 준비 확인, 웹훅 인증 확인, Telegram 조회/등록)와 안전한 오류 코드를 출력합니다. `DNS_ERROR`는 주소 해석 실패, `TIMEOUT`은 시간 초과, `TLS_CERTIFICATE_ERROR`는 인증서 검증 실패, `PROXY_ERROR`는 프록시 연결 오류입니다. 예외 원문이나 비밀값은 출력하지 않습니다. 인증서 검증을 끄지 말고 원인을 해결하세요.

먼저 비밀값이 필요 없는 주소를 브라우저에서 확인합니다.

```text
https://tenniswatch-telegram-worker.tenniswatch-kkh.workers.dev/health
```

`ok`가 보이는지 확인하고 `register-webhook`을 다시 실행합니다. 브라우저도 연결되지 않으면 대시보드의 배포 주소와 현재 네트워크의 DNS/VPN/프록시를 확인합니다. 이 오류 진단 변경 후 Python 전체 80개 테스트가 통과했습니다. 에이전트 실행 환경에서는 Worker 상태 확인 단계의 `DNS_ERROR`를 확인했으며, 사용자의 터미널에서도 같은 원인인지는 재실행 결과로 구분해야 합니다.

`/health` 성공 후 `/ready`가 HTTP 503이면 아래 읽기 전용 진단으로 Supabase를 직접 확인합니다. LLM 호출이나 DB 변경은 하지 않습니다.

```bash
.venv/bin/tenniswatch check-worker-db
.venv/bin/alembic current
```

- `READINESS_FUNCTION_MISSING`: 해당 Supabase 프로젝트에 `0007`을 적용했는지 확인합니다. DB URL과 API URL이 서로 다른 프로젝트인지도 확인합니다.
- `INVALID_API_KEY`: 해당 프로젝트의 legacy `service_role` 키를 확인합니다.
- `DB_PERMISSION_DENIED`: 서비스 역할의 테이블 및 함수 접근 권한을 확인합니다.
- `DB_TABLE_MISSING` / `DB_COLUMN_MISSING`: 마이그레이션 적용 대상과 버전을 확인합니다.
- `INVALID_SUPABASE_URL`: `/rest/v1` 경로 없이 프로젝트 HTTPS 기본 주소만 입력합니다.
- `READY`: 로컬 DB 연결은 정상입니다. Worker에 올린 설정과 로컬 설정의 차이 또는 Worker 설정 검증·네트워크 오류를 점검합니다. 이 결과만으로 Worker 준비 완료를 의미하지는 않습니다.

진단 결과는 허용된 상태 코드만 출력하며 API 응답 원문과 키를 출력하지 않습니다.

### 웹훅 등록 후 대화 응답이 없을 때

```bash
.venv/bin/tenniswatch worker-status
```

최근 업데이트 30건과 AI 사용량 30건의 상태별 개수, 안전한 오류 코드, Telegram 미전달 건수만 조회합니다. 메시지 본문·답변·사용자 식별자·키·외부 오류 원문은 출력하지 않으며 AI 호출이나 DB 변경은 없습니다. 표본이 최근 30건이므로 전체 대기열 크기와 같지는 않습니다.

- `telegram_updates.recent_count=0`: 웹훅 전달 실패나 허용 개인 채팅 설정을 확인합니다. Telegram의 마지막 전달 오류는 과거 실패가 남아 있을 수 있으므로 현재 미전달 건수와 함께 봅니다.
- 업데이트는 `pending`이고 AI 사용 기록이 없으면 Worker Cron 실행 및 claim 오류를 확인합니다. Cloudflare의 Cron 설정 변경은 반영까지 시간이 걸릴 수 있습니다.
- `llm_usage`에 `FAILED`가 있으면 모델 API 호출이 실패했습니다. 이 요약만으로 키·모델 권한·사용량 한도·결제·네트워크 중 정확한 원인을 단정하지 않습니다.
- AI는 `COMPLETE`인데 업데이트가 실패하면 도구 실행·문맥 저장·Telegram 발송 등 후속 단계를 점검합니다.
- `failed` 업데이트는 최대 시도를 소진한 상태입니다. 원인 해결 전 기존 작업을 무작정 재시도하지 않습니다.

### 기존 Python 사용자와 AI 대화 연결 수정

Python 구독으로 생성된 사용자는 `telegram_user_id`가 비어 있을 수 있습니다. 기존 Worker는 이때 사용자 INSERT/갱신을 시도하면서 DB 필수값 `enabled`를 생략했습니다. 문맥 로딩은 AI 호출 전에 수행하므로 이 경로에서 실패하면 AI 사용 기록 없이 재시도만 반복될 수 있습니다.

수정한 Worker는 기존 개인 채팅의 사용자 행을 찾아 미설정 식별자만 조건부 연결하고 `enabled`는 보존합니다. 새 사용자 INSERT에는 `enabled=true`를 포함하며, 동시에 생성된 기존 행을 덮어쓰지 않습니다. 다른 식별자에 연결된 채팅은 거부합니다. 이 수정은 Worker 재배포가 필요하며 DB 마이그레이션·웹훅 재등록은 필요하지 않습니다.

이후 처리 실패는 `CONTEXT_LOAD_FAILED`, `AGENT_FAILED`, `CONTEXT_SAVE_FAILED`, `REPLY_STORE_FAILED`, `TELEGRAM_SEND_FAILED`, `INBOX_COMPLETE_FAILED`로 구분합니다. 기존 실패 기록의 `PROCESSING_FAILED`는 그대로 남습니다. 시도 횟수를 소진한 메시지는 자동 복구하지 않으므로 배포 후 새 메시지 한 건으로 먼저 확인합니다.

### AI 호출 실패 코드

AI 서비스 실패 시 응답 원문 대신 아래 코드만 `llm_usage`에 저장합니다. 이 변경은 Worker 재배포 후 새로 발생한 오류부터 적용됩니다.

- `LLM_AUTH_FAILED`: API 키 인증 실패(401).
- `LLM_ACCESS_DENIED`: 모델·프로젝트 접근 거부(403).
- `LLM_MODEL_NOT_FOUND`: 모델 또는 API 경로를 찾지 못함(404).
- `LLM_QUOTA_EXCEEDED`: 공급자가 `insufficient_quota`를 반환함. API 크레딧·결제·사용 한도를 확인합니다. ChatGPT 구독만으로 API 사용량이 제공된다고 가정하지 않습니다.
- `LLM_RATE_LIMITED`: 429 제한 응답이며, 위 크레딧 오류로 명시되지 않음.
- `LLM_INVALID_REQUEST`: 요청 형식 또는 모델 호환성 확인 필요(400/422).
- `LLM_NETWORK_FAILED` / `LLM_PROVIDER_ERROR`: 네트워크·시간 초과 또는 기타 공급자 오류.

인증·접근·모델·크레딧·요청 형식 오류는 같은 메시지로 반복 모델 호출하지 않고 실패 안내를 저장해 사용자에게 보냅니다. 작업 완료를 주장하지 않습니다. 일시적 호출 제한과 네트워크 오류는 기존 제한 횟수 안에서 재시도합니다. 이전 기록의 `LLM_REQUEST_FAILED`만으로 실패 원인을 단정할 수 없습니다.

### 공급자 변경 후에도 자체 AI 한도 안내가 나오는 경우

TennisWatch의 일일 예산은 공급자와 관계없이 합산됩니다. 실패한 모델 호출도 호출 수에 포함되고 실제 토큰 사용량을 받지 못한 요청은 보수적인 예약 토큰을 유지합니다. Gemini로 바꾸거나 API 키를 교체해도 이 기록은 초기화하지 않습니다.

`worker-status`의 `ai_budget_today`에서 오늘의 전체 호출 수(`global_calls`), 로컬에서 연결한 개인 사용자의 호출 수(`linked_user_calls`), 실제/예약 합산 토큰(`accounted_tokens`), 다음 한국 시간 자정(`resets_at`)을 확인합니다. 최대 1,000행 표본이며 `sample_limit_reached=true`일 때 수치는 하한입니다. 사용자별 카운트는 기존 개인 연결의 chat ID와 Telegram 사용자 ID가 같은 경우를 기준으로 합니다.

Worker 설정 한도는 사용자 24회·전체 100회·토큰 150,000개입니다. 다음 호출의 예약량까지 합산하므로 합산 토큰이 150,000 미만이어도 거절될 수 있습니다. 환경 변수를 생략한 코드·DB 기본값은 100,000개이므로 배포 시 wrangler.jsonc의 값을 확인합니다. 이 설정은 실제 모델 요금과 같지 않습니다. 진단 없이 예산을 무제한으로 늘리거나 사용 이력을 삭제하지 않습니다. 자체 한도는 한국 시간 자정에 새 날짜 기준으로 계산되며 공급자의 한도는 별도로 적용됩니다.


2026-10-25 연결 점검에서 실패 호출 15회·예약 토큰 94,215개로 기존 100,000개 한도가 다음 호출을 거절했습니다. 실패 기록은 보존하고 `wrangler.jsonc`의 토큰 예산만 150,000개로 조정했습니다. 사용자별 20회·전체 100회 제한은 유지합니다. 이 변경은 배포 후 적용되며 공급자 자체 한도와 별개입니다. 모델 도구 호출 한 번의 대화에 여러 호출이 필요할 수 있어 남은 5회가 메시지 5개를 의미하지는 않습니다. 실제 성공을 확인하면 정상 운영 예산을 사용량에 맞춰 다시 정합니다.

### Gemini 모델을 찾지 못하거나 연결 설정 오류가 반복될 때

```bash
.venv/bin/tenniswatch check-gemini-models
.venv/bin/tenniswatch worker-status
```

첫 명령은 로컬 키로 Google의 `GET /v1beta/models` 메타데이터만 조회합니다. 키는 헤더로 보내고 출력하지 않으며 콘텐츠 생성과 앱 AI 예산 예약을 수행하지 않습니다. 모델 이름 후보와 현재 모델이 목록에 있는지 여부만 출력합니다. 모델 목록에 나타난다고 실제 생성 권한·무료 한도·도구 호출 성공까지 보장하지는 않습니다. 목록에는 최대 3페이지·1,000개/페이지를 요청하며 출력 후보는 40개로 제한합니다. 외부 설명 문구는 출력하지 않습니다.

`configured_model_listed=false`이면 반환된 후보를 기준으로 모델을 선택합니다. `INVALID_API_KEY`이면 Google AI Studio에서 발급한 키와 API 제한 설정을 확인합니다. 다른 모델 이름을 추측하며 콘텐츠 생성 요청을 반복하지 않습니다. 실제 생성 실패 원인은 `worker-status`의 새 `llm_usage.errors` 코드로 별도 구분합니다.


### Gemini 도구 호출 서명 보존

2026-10-25 모델 조회 성공 후 AI 1회 성공·후속 요청 형식 오류를 확인했습니다. 코드에서는 모델 응답의 `tool_calls[].extra_content.google.thought_signature`가 유실되고 있었습니다. Google 문서는 이 값을 도구 실행 결과를 전달하는 후속 요청에 원형 그대로 포함해야 하며 누락 시 400 오류가 날 수 있다고 안내합니다. [공식 설명](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)

현재 코드는 해당 서명 필드만 검증해 동일 대화 왕복 동안 전달합니다. 임의의 추가 필드는 버리고 서명을 도구 인자·사용자 답변·문맥 DB·로그에 저장하지 않습니다. 네트워크 없이 실제 AgentRunner와 호환 클라이언트를 연결한 모의 왕복 테스트를 추가했습니다. 실제 Gemini 성공은 재배포 후 확인해야 합니다.

누적 호출 19회 상태에서 최소 2회 모델 왕복을 확인할 수 있도록 Worker 사용자 한도를 임시 24회로 조정했습니다. 전체 100회·토큰 150,000개는 유지합니다. 코드·DB의 생략 시 기본값 20회와 배포 변수의 24회는 구분합니다. 키 교체·웹훅 재등록·DB 이력 삭제는 필요하지 않습니다. 연결 검증 후 정상 운영 한도를 다시 결정합니다.


### AI 연결 오류 반복 시 (2026-10-26 보완)

기존 `LLM_NETWORK_FAILED`는 요청의 15초 시간 초과도 포함했습니다. 새 Worker는
요청 또는 성공 응답 본문 읽기의 시간 초과를 `LLM_TIMEOUT`으로 분리합니다.
기존 오류 이력으로 실제 네트워크 장애와 시간 초과를 소급 구별할 수 없습니다.
연결 오류/시간 초과는 첫 처리 실패에서 재시도하고, 두 번째 이후 처리 시도에서도 실패하면
안내 답변을 저장·발송해 모델 재호출을 멈춥니다. 발송 실패 재시도는 저장한 답변을 재사용합니다.
도구 실행 후 실패했을 수도 있으므로 안내는 변경이 전혀 없었다고 단정하지 않습니다.
실패 호출의 예약 예산은 계속 보수적으로 집계하며 한도를 자동으로 높이지 않습니다.

```bash
cd apps/telegram-worker
npx wrangler deploy
```

DB 마이그레이션·Secret 재업로드는 필요하지 않습니다. 재배포 후 `worker-status`로 새 오류 코드와
대기 감소를 확인합니다. `processed`는 실패 안내 전달을 포함하며 AI 작업 성공과 같지 않습니다.
사용자의 반복 실패는 확인했으나 공급자 연결 장애의 원인은 아직 확정하지 못했습니다.
Worker 테스트 78개·타입 검사, Python 테스트 202개·문법 검사로 검증했습니다.

요청 제한은 15초로 유지합니다. [Cloudflare 공식 실행 제한](https://developers.cloudflare.com/workers/platform/limits/)의
Cron 실행 시간 제한과 별개로, 현재 DB 작업 잠금은 2분이므로 모델 요청 제한만 임의로 늘리지 않습니다.

### 대기 상태가 바뀌지 않을 때

재시도는 실패 횟수에 따라 1·2·4·8분 뒤 예정됩니다. Worker 재배포는 저장된 next_attempt_at을 앞당기지 않습니다. worker-status의 telegram_updates.waiting은 최근 30건 중 pending/processing의 attempts, next_attempt_at 또는 lease_until, due, seconds_until_due를 출력합니다. checked_at은 진단 기준 UTC 시각입니다. due=false면 아직 예정 시각 전이며, due=true인 pending이 여러 정기 실행 동안 유지되면 Cron 실행과 DB claim 오류를 점검합니다. processing의 due는 처리 잠금 만료 여부입니다. 표본에 없는 대기 건은 포함되지 않으며 메시지·답변·개인 식별자는 조회하거나 출력하지 않습니다. Python 203개 테스트와 문법 검사 통과.
