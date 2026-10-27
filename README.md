# TennisWatch AI

Worker Cron → GitHub `workflow_dispatch` 전환: 시간당 취소표 감시와 오전 9시 일일 보고를 분리했습니다. `0010` 적용과 Worker Secrets 등록 후 스케줄러를 활성화하세요. [배포·실행 확인·복구 가이드](docs/worker-scheduler.md).

Telegram 자연어 대화와 Python 수집기를 사용하는 공공 테니스장 정보 비서입니다. 전국 확장이 목표이며, 강서 세 시설의 실제 코트 목록 발견과 인천 계양 수집 어댑터를 제공합니다. 실제 등록·수집 성공 여부는 시설별 기록으로 확인합니다. 예약을 자동으로 수행하지 않습니다.

## 현재 상태

- 알림 대시보드: `/dashboard`에서 본인 감시 조건·매일 보고 시각·수집/발송 이력을 조회합니다. 새 Worker 배포 후 봇의 `/dashboard` 명령으로 15분 접속 코드를 발급받습니다. 실제 화면 조회는 배포 후 확인이 필요합니다. [이용·보안 안내](docs/dashboard.md).

- 매일 지정 시각의 예약 가능 코트 보고 기능을 추가했습니다. 기존 감시 조건 기준이며, `0008` 적용 및 GitHub Actions에서 보고 생성·Telegram API 전송 성공을 확인했습니다. 신규 사용자는 시각 설정과 Secrets 등록이 필요합니다. 자연어 설정 관리에는 Worker 배포가 필요합니다. [설정 방법](docs/daily-reports.md).

- Phase 7: Cloudflare 배포·Telegram 웹훅·Gemini 도구 호출·Supabase 감시 조건 조회까지 사용자가 실제 대화로 확인했습니다. 이후 GitHub Actions 감시 재실행에서 수집 1회·슬롯 관측 3개·오류 0개, 일일 보고 Telegram API 전송 성공 1건을 확인했습니다. 사용자 단말 수신 확인은 별도입니다. [배포·운영 안내](docs/deployment.md).

- Phase 1 분석: 기존 Python 수집기와 모니터링 기반을 확인하고, 확장 아키텍처를 `docs/phase1-architecture.md`에 정리했습니다.
- 신규 구성은 Supabase PostgreSQL, SQLAlchemy 2.x/Alembic, GitHub Actions, Telegram Bot API를 기준으로 합니다.
- 강서 어댑터는 우장산근린공원·강서구립·황금내근린공원 테니스장의 공식 진입 링크와 실제 선택 목록을 사용합니다. `discover-gangseo`로 발견한 코트만 `init-gangseo-all`로 등록합니다. 기존 우장산 1코트 구독은 보존합니다. 실제 코트별 잔여석 수집은 추가 실행으로 확인해야 합니다. [확인 내용과 점검 절차](docs/gangseo-collector.md)를 참고하세요.

## 아키텍처

`collectors`는 사이트별 HTTP/API/브라우저 처리를 맡고, `services`는 일정 계산·상태 전이·알림 발송을 담당합니다. 도메인 로직은 네트워크와 데이터베이스에서 분리해 mock으로 검증할 수 있게 유지합니다. PostgreSQL unique 제약조건과 advisory lock을 이용해 이벤트/작업 중복을 줄이고, 전송 전 상태를 기록해 프로세스 중단 시 중복 가능성이 남는 Telegram 전송 특성을 문서화합니다.

## 개발 설정

Python 3.12 이상이 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

운영 환경 변수는 `.env.example`을 참고하세요. 실제 값은 GitHub Actions Secrets에 등록하고 저장소에 커밋하지 마세요.

## 비용 및 운영 한도

GitHub Actions와 Supabase는 무료 제공량, 유휴 프로젝트 정책, 저장 용량 및 실행 시간 정책이 변경될 수 있습니다. 30분 간격 실행은 기본 60분보다 Actions 사용량이 대략 두 배입니다. 월 0원은 사용량이 무료 한도 안에 있을 때만 가능합니다. 예약 수집은 사이트별 설정 주기와 구독이 존재하는 시설에만 실행하도록 설계합니다. Worker가 실행을 요청하고 GitHub가 Python Collector를 실행합니다. 실행 대기와 수집 시간 때문에 정확한 보고 수신 시각을 보장하지 않습니다.

공개 저장소를 사용하더라도 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL` 및 사용자 식별 정보를 로그/소스에 기록하지 마세요. Telegram chat id는 비밀로 취급합니다.

## Telegram Worker (Phase 2)

Telegram 웹훅 수신과 중복 업데이트 방지용 Worker가 `apps/telegram-worker`에 있습니다. 메시지는 Supabase의 `telegram_updates`에 저장한 뒤, 1분 간격 Worker 작업이 LLM과 DB 도구를 호출합니다. 최근 수집 실패와 오래된 관측을 구분해 안내합니다.

```bash
cd apps/telegram-worker
npm install
npm test
npm run typecheck
```

배포 전에 DB 마이그레이션을 적용하고 Cloudflare Worker Secrets에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `LLM_API_KEY`, `LLM_MODEL`, `ALLOWED_TELEGRAM_CHAT_IDS`를 등록해야 합니다. 최신 절차는 배포 문서를 따르세요. `LLM_MODEL`은 공급자에서 지원하는 모델 이름으로 Worker 변수에 설정하고, 다른 공급자가 같은 Chat Completions 형식을 지원한다면 `LLM_API_URL`에 전체 호환 경로를 지정합니다. 다른 형식의 API는 별도 연결부가 필요합니다. `SUPABASE_SERVICE_ROLE_KEY`와 `LLM_API_KEY`는 Worker 안에서만 사용하고 공개하지 마세요. `wrangler deploy` 후 Worker 주소의 `/telegram/webhook` 경로를 Telegram `setWebhook`에 등록하고, `allowed_updates`를 `message`로 제한합니다. 로컬 테스트는 Vitest 모의 데이터만 사용하며 실제 Telegram/Supabase/LLM 연결은 검증 대상에 포함되지 않습니다.

## 실행 및 마이그레이션

마이그레이션, 시설 등록, 잔여석 감시와 알림 재시도, 검증된 DB 정책의 예약 오픈 알림 명령을 제공합니다. 최신 마이그레이션은 `0009`입니다.

```bash
alembic upgrade head
tenniswatch init-gangseo
tenniswatch collect-slots
tenniswatch watch-slots
tenniswatch check-policies
# 발송 대기열만 별도로 처리: tenniswatch send-outbox
```

수집 대상이 0이고 아직 감시 조건을 등록하지 않았다면, 개인 봇 채팅을 연결한 뒤 날짜와 시간을 직접 등록할 수 있습니다.

```bash
.venv/bin/tenniswatch link-telegram
.venv/bin/tenniswatch subscribe-gangseo --date 2026-10-26 --start-time 18:00 --end-time 22:00
.venv/bin/tenniswatch collection-plan
.venv/bin/tenniswatch watch-slots
```

첫 명령은 기존 웹훅 여부를 먼저 검사하고 본인 봇 주소와 일회용 확인 문구를 표시합니다. 터미널을 켜 둔 채 3분 안에 한 줄 전체를 봇의 개인 채팅으로 보내면 `.env`의 `TELEGRAM_CHAT_ID`에 저장하고 봇에서도 확인 답장을 보냅니다. 대기 중에는 진행 상태를 표시합니다. 토큰과 채팅 ID는 출력하지 않습니다. 기존 웹훅을 삭제하거나 메시지를 수신 완료로 처리하지 않습니다. 다른 수신 작업이 있는 경우에는 연결이 거절될 수 있습니다. 예시 날짜·시간은 원하는 조건으로 바꾸세요. 날짜는 오늘부터 31일 이내이며 실제 수집은 기본 7일 범위 안에 들어왔을 때 수행합니다. 같은 조건을 다시 등록해도 중복 생성하지 않습니다.

## 지원 범위

예약 사이트별 adapter는 공개된 데이터만 사용합니다. CAPTCHA·로그인 우회나 과도한 요청은 하지 않습니다. 검증되지 않은 예약 정책은 확정 알림에 쓰지 않습니다. 수집 실패는 마감으로 처리하지 않습니다.

## 구현 진행 및 미완료 항목

### Phase 1 — 분석 및 아키텍처

- 완료: 기존 Python 코드와 DB/워크플로 분석, Worker와 collector 분리 설계 및 운영 기준 문서화.
- 변경: `src/tenniswatch`, `alembic`, `.github/workflows` 및 의존성/환경변수 골격.

### MVP 기반 구현

- 추가: 예약 정책/일정, 사이트/시설/코트, 슬롯/이벤트, 사용자/구독, 알림 outbox, 수집 실행 테이블.
- 추가: 구독 시설/요일/시간 필터, 첫 관측 포함 상태 전이, 이벤트 단위 중복 방지, PostgreSQL/SQLite용 멱등 outbox 삽입, SKIP LOCKED 발송 claim, 15분 lease 회수, 지수형 재시도, Telegram 성공/실패 상태 기록.
- `tenniswatch send-outbox`는 DB의 대기 알림을 실제 Telegram Bot API로 발송합니다. 토큰과 기본 chat id가 필요하며 구독자별 chat id를 사용합니다.
- Phase 6에서 `check-policies`와 검증 정책의 오픈 일정 알림을 연결했습니다. Telegram 응답 성공 후 DB 저장 전에 프로세스가 종료되면 API에 멱등성 키가 없어 중복 발송 가능성이 남습니다.

### 검증 현황

- `.venv/bin/pytest -q`: 185개 통과 (모의 DB·HTML·HTTP, 강서 세 시설 발견·등록·기존 구독 보존 포함).
- Worker `npm test`: 74개 통과, `npm run typecheck`: 통과.
- `.venv/bin/python -m compileall -q src tests alembic`: 통과.
- Phase 5 실제 Chrome에서는 공개 예약 페이지가 열렸지만 센터 목록이 불러오기 오류로 끝났습니다. 로컬 실행은 DNS 접근 실패와 Chromium 미설치로 실제 시간표 수집을 검증하지 못했습니다. 이후 사용자는 Cloudflare·Telegram·Gemini·Supabase를 연결해 기존 감시 조건 조회 응답을 확인했습니다. Actions 정기 실행과 신규 잔여석 알림까지 검증된 것은 아닙니다.

### Phase 2 — Telegram 웹훅 기반

- 추가: Cloudflare Worker 골격, Telegram 비밀 토큰 헤더 검사, 개인 대화 텍스트만 허용, Supabase inbox 기반 update_id 중복 방지, 1분 간격 임시 접수 답변, 실패 재시도.
- 추가: inbox와 `SKIP LOCKED` 작업 claim용 PostgreSQL 마이그레이션 `0003`. 기존 DB에는 `alembic upgrade head`로 적용합니다.
- 당시 임시 접수 답변은 Phase 3의 AI 응답으로 교체됐습니다. Telegram 전송이 성공한 직후 DB 상태 기록 전에 프로세스가 종료되면 제한적으로 중복 응답이 발생할 수 있습니다.

### Phase 3 — AI Agent 기반

- 추가: OpenAI 호환 Chat Completions API 클라이언트, 모델 도구 호출 왕복, 날짜/시간 및 도구 인자 검증, Seoul 기준 프롬프트, 입력/출력 토큰 사용량 로그, 도구 호출/응답 크기 제한.
- 추가: 시설/잔여석/오픈일정 조회와 구독 생성·수정·삭제·조회 등 7개 도구 설명.
- 도구의 실제 DB 동작과 사용자별 대화 문맥은 Phase 4에서 연결했습니다. 실제 LLM API 호출은 아직 검증하지 않았습니다.

### Phase 4 — Supabase 도구와 사용자 문맥

- 추가: 시설 검색, 날짜별 잔여석 조회, 검증된 예약 오픈 일정 조회, 사용자별 감시 조건 CRUD, 24시간 대화 문맥 저장.
- 추가: `0004`에서 사용자 Telegram ID, 시설 지역/지원 상태, 사용자 감시 조건, 감시 시설 연결, 대화 문맥, 도구 실행 멱등성 테이블과 트랜잭션 RPC를 확장합니다.
- 실제 발견·등록한 지원 시설을 도구로 조회합니다. 새 조건은 `PENDING_ACTIVATION`으로 저장되며 Phase 6 감시 작업의 첫 확인 후 `ACTIVE`로 전환됩니다.

### Phase 5 — 수집 경로와 데이터 신뢰도

- 기존 `subscriptions`와 새 `user_watches`에서 시설·날짜를 추출하고 중복을 제거합니다. 비활성·만료·미지원 대상은 제외하며 `reservation_sites.poll_minutes`보다 자주 수집하지 않습니다(최소 30분).
- 브라우저를 필요한 경우에만 시작하고 날짜별 페이지 조회는 최소 5초 간격으로 제한합니다. 허용된 HTTPS 호스트만 요청합니다. 접근 거부·빈 표·알 수 없는 상태는 수집 실패입니다.
- 시설/코트/날짜 선택 상태와 시간표 행 로딩을 확인한 뒤 날짜 단위로 원자적으로 저장합니다. 실패 시 이전 슬롯과 관측 시각을 보존하며 더 오래된 관측으로 새 상태를 덮어쓰지 않습니다.
- `0005`는 시설별 마지막 수집 성공 시각과 수집 실행의 시설·요청 날짜·실행 시간·오류 코드를 추가합니다. 실제 DB 적용은 `alembic upgrade head`로 진행합니다.
- `SUPPORTED`는 어댑터가 등록되었다는 뜻입니다. 실제 성공 기록은 `last_collected_at` 또는 조회 결과의 `last_successful_collection_at`으로 확인합니다. 현재 환경에서 실제 성공 기록을 생성하지 않았습니다.
- `collect-slots`는 Telegram 발송 없이 저장하며 대기 상태인 조건을 활성화하지 않습니다. 이미 활성인 조건의 이벤트와 알림 대기열은 생성할 수 있습니다.

### Phase 6 — 자동 알림 경로

- `watch-slots`는 새 감시 조건을 첫 수집 성공 후 활성화하고, 날짜·요일·현지 시간대에 맞는 잔여석을 사용자별로 알립니다. 같은 이벤트는 한 사용자에게 한 번만 발송하고 마감 후 재개방은 새 이벤트로 처리합니다.
- `check-policies`는 구독된 시설의 공식 정책을 갱신하고 검증된 정책으로 향후 1년의 오픈 일정을 저장하고 실제 오픈 시각 기준 전날·1시간 전 알림을 관리합니다. 오픈 시점 알림은 사용자가 요청한 경우에만 설정합니다. 실행 지연을 고려한 오픈 후 유예는 3시간이며 그 뒤에는 늦은 알림을 버립니다. 정책 내용 변경·확인 실패·36시간 이상 오래된 출처는 확정 발송을 보류합니다. 현재 실제 정책 자동 수집 어댑터는 계양만 지원합니다.
- 계양의 최초 정책 확인은 마이그레이션 후 `.venv/bin/tenniswatch init-gyeyang-opening`으로 실행하고, 개인 진행 상태는 `.venv/bin/tenniswatch opening-status`로 확인합니다. Telegram 자연어 구독은 기본 세 알림 시점을 사용하며 특정 시점만 요청할 수 있습니다.
- 발송 직전에 구독·현재 슬롯·관측 시각·수집 실패·정책·오픈 일정 버전을 다시 확인합니다. 발송 중 작업 잠금과 고유 제약을 사용하고, 오류는 최대 8회까지 재시도합니다. 봇 차단(403)은 해당 사용자의 알림을 비활성화합니다.
- 잔여석 작업은 기본 매시 7분, 예약 오픈 작업은 매시 17분입니다. Worker가 두 작업을 각각 요청하며 수동 실행도 지원하고 LLM을 호출하지 않습니다. 일일 보고는 한국 시간 오전 9시 별도 요청합니다. 슬롯·보고 실행에는 Chromium을 설치합니다.
- 작업 코드는 구현됐지만 이 환경에서 실제 Actions 실행, Supabase `0009` 적용, 정책 최신 확인 및 Telegram 오픈 알림 수신을 검증한 것은 아닙니다. [설정·실행·복구 방법](docs/monitor-operations.md)을 참고하세요.

### 강서 사이트 어댑터 상태

공개 달력 URL과 센터·시설·장소 선택 필드, 날짜 필드, 예약 시간 선택 표를 확인했습니다. 2026-10-24 재확인에서는 센터 목록 오류로 선택된 코트의 실제 시간표를 확인하지 못했습니다. CAPTCHA를 읽거나 입력하지 않으며 예약자·팀 정보를 저장하지 않습니다. 시간대 상태나 페이지 문맥을 확인할 수 없으면 기존 슬롯을 유지합니다.

기존 우장산 1코트와 함께 우장산근린공원·강서구립·황금내근린공원의 실제 발견 코트를 지원합니다. [공식 시설별 예약 링크](https://sports.gangseo.seoul.kr/fmcs/64)에서 진입 식별자를 확인했으며 황금내의 시설 구분값은 `C`입니다. 다른 코트 번호를 추정하지 않고 선택 목록을 검증합니다.

최초 등록은 마이그레이션 후 한 번 실행합니다.

```bash
tenniswatch init-gangseo
```

위 명령은 기존 1코트 호환용입니다. 세 시설의 전체 코트를 발견·등록하려면 다음 명령을 사용합니다. 등록은 잔여석 확인과 별개이며 기존 구독 범위를 자동 확대하지 않습니다.

```bash
.venv/bin/tenniswatch discover-gangseo
.venv/bin/tenniswatch init-gangseo-all
```

수집은 활성화된 기존 구독 또는 새 감시 조건에 연결된 시설만 대상으로 합니다. 기존 구독의 요일은 ISO 숫자(월요일 `1`~일요일 `7`, 쉼표 구분), 시간은 `HH:MM`입니다. 새 감시 조건은 특정 날짜 또는 요일 목록을 사용합니다. `WATCH_DAYS_AHEAD` 기본값은 7일이며 최대 31일입니다.

```bash
tenniswatch watch-slots
```

`watch-slots`는 Actions 환경에서 Chromium을 설치해 한 브라우저 세션으로 구독한 날짜를 차례로 조회합니다. 로컬 pytest는 HTTP/HTML mock만 사용하며 실제 사이트 호출은 하지 않습니다. 현재 개발 환경에서 Playwright Chromium은 설치되어 있지 않아 로컬 브라우저 수집 실행은 검증하지 못했습니다. GitHub Actions Workflow에는 Chromium 설치 단계를 추가했습니다.

DB와 Telegram에 쓰지 않는 실제 페이지 점검 명령도 제공합니다. 먼저 Chromium을 설치하고 확인할 날짜를 지정하세요.

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python -m tenniswatch.cli probe-gangseo --date 2026-10-24
```

날짜는 실제로 확인하려는 날짜로 바꿉니다. 오류가 발생하면 이를 예약 마감으로 기록하지 않습니다.

### Supabase 사용자/구독 등록 예시

서비스는 DB 연결을 통해 알림 구독을 읽습니다. `TELEGRAM_CHAT_ID`는 Telegram의 `@userinfobot` 등으로 확인한 개인 chat id를 입력하세요. 공유 링크나 저장소에는 넣지 마세요.

```sql
-- 한 번만 등록하고 반환된 id를 확인합니다.
INSERT INTO telegram_users (chat_id, enabled)
VALUES ('<YOUR_TELEGRAM_CHAT_ID>', true)
ON CONFLICT (chat_id) DO UPDATE SET enabled = true
RETURNING id;

-- 시설 id는 아래 조회 결과를 사용하고, 요일은 ISO 월=1 ... 일=7 입니다.
SELECT id, name FROM facilities WHERE external_id = 'GANGSEO02:T:03:2';

INSERT INTO subscriptions (user_id, facility_id, weekdays, start_time, end_time, enabled)
VALUES (<USER_ID>, <FACILITY_ID>, '6,7', '09:00', '18:00', true);
```

보안 마이그레이션 `0002`는 모든 앱 테이블에서 Supabase REST의 익명/로그인 역할을 RLS로 차단합니다. 앱의 비밀 `DATABASE_URL`로 연결하는 postgres 역할은 운영을 계속할 수 있습니다. 새 코드를 받은 뒤 `alembic upgrade head`를 실행해 적용하세요.

### 인천 계양테니스장 잔여석 수집

실내·실외 각 3면의 선택 목록과 날짜 전체 시간표를 검증하는 어댑터 및
`probe-gyeyang --date YYYY-MM-DD`, `init-gyeyang --date YYYY-MM-DD` 명령을 추가했습니다.
실제 점검 성공 전 시설을 자동 등록하지 않습니다. 2026-10-26 사용자 실행에서
실내·실외 6면의 96개 시간대 수집 성공을 확인했습니다. 모두 예약 가능 아님으로
나왔으며 예약 예정도 포함하므로 매진으로 해석하지 않습니다. 후속 사용자 실행에서
DB 등록과 수동 감시(대상 2곳·관측 99개·오류 0개)도 성공했습니다.
계양 정기 운영, 실제 예약 가능 슬롯 판정 및 알림 발송은 별도 확인이 필요합니다.
[정책 조사·실행 방법·지원 제한](docs/gyeyang-reservation-policy.md)을 확인하세요.
HTTP 응답의 동적 선택 목록이 비어 있으면 Playwright로 로딩을 기다립니다.
최초 실행 전에 `.venv/bin/python -m playwright install chromium`으로 브라우저를 설치하세요.

우장산 전체 4코트 등록은 `.venv/bin/tenniswatch init-gangseo-all`을 사용합니다. 실제 목록 4개가 모두 확인돼야 등록하며 코트 식별자를 추정하지 않습니다. 기존 1코트 감시 조건은 자동 확대되지 않습니다. 등록 후 원하는 날짜·시간대로 전체 4코트 감시를 요청하고 감시 목록을 확인하세요. 목록 조회 오류의 단계와 실제 검증 범위는 [강서 수집 문서](docs/gangseo-collector.md)를 참고하세요.

황금내 등 다른 시설의 목록 오류로 전체 등록이 중단되면 우장산만 명시해 등록할 수 있습니다: `.venv/bin/tenniswatch init-gangseo-all --facility-group GANGSEO02:T:03`. 실제 4코트 검증 조건은 동일합니다.
