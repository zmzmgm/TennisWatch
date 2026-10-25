# TennisWatch AI — Phase 1 analysis and architecture

현재: Phase 6의 새 감시 조건 알림, 검증된 DB 정책의 오픈 알림 및 두 Workflow를 구현했습니다. 최신 마이그레이션은 `0006`입니다. 실제 배포·정기 실행·수신까지의 전체 검증과 비용 점검은 Phase 7에서 진행합니다. 사이트 정책 자동 수집/갱신은 미지원입니다.

## 1. Current project inventory

The repository already has a Python 3.12 package, SQLAlchemy 2 models, Alembic migrations, GitHub Actions schedules, and offline unit tests. It is therefore best to retain the existing collector/monitor as the data plane and add a separate TypeScript Worker as the conversational control plane.

| Area | Existing implementation | Gap for TennisWatch AI |
|---|---|---|
| Domain | `src/tenniswatch/domain.py` has availability states, transition decisions, and a basic opening-date helper | Opening-policy rules need source, verification/effective dates, priority groups, and confidence/validity checks |
| Persistence | `models.py`, SQLAlchemy, Alembic; facilities, courts, sites, policies, schedules, slots, events, users, subscriptions, notifications, collection runs | No Telegram sender identity, webhook update ledger, conversations, agent tool idempotency, or multi-facility natural-language watch criteria |
| Availability | `services.py` compares observations, creates events/outbox rows, and claims notifications with PostgreSQL row locking | Extend delivery/idempotency semantics and filter against normalized watch criteria; preserve last state on collection failure |
| Telegram | `notifications.py` sends availability notices through Bot API | No inbound webhook or conversational reply handling |
| Collection | `collectors/base.py` and registry; `collectors/gangseo.py` is read-only and supports Wujangsan Tennis Court 1 | Nationwide support is not implemented; runtime/browser compatibility and additional sites need separate verification |
| CLI/workflows | Hourly `watch.yml`; daily `policies.yml`; Python CLI includes `watch-slots`, `send-outbox`, and policy command | `check-policies` is currently a stub; current hourly workflow installs Playwright Chromium and should avoid doing so for HTTP-only adapters |
| Tests | Python pytest suite with mocked/offline coverage | Add TypeScript/Vitest tests with Worker implementation; live-site integration remains a separately labeled check |

The repository has no Worker/TypeScript application yet. The local `.env` must remain untracked; never print or copy its contents. Only the supported Gangseo facility above may be presented as live support. Existing tests establish core offline behavior, not end-to-end delivery or live scraping correctness.

## 2. Proposed architecture

```text
Telegram --webhook--> Cloudflare Worker (TypeScript)
                           | verify secret, dedupe update, persist inbox
                           | bounded LLM tool calling; validated tools only
                           v
                    Supabase PostgreSQL
                           ^             |
                           |             +--> tool results, subscriptions, context
             SQLAlchemy / Alembic       +--> transactional event/notification outbox
                           |
GitHub Actions --> Python collector --> availability state/events --> Telegram Bot API
```

The Worker is the user-facing control plane. The Python monitor is the data plane. Both share Postgres, not process memory or an ad hoc Worker-to-collector API. Scheduled collector runs never invoke an LLM.

### Webhook processing and reliability

1. Validate Telegram's `X-Telegram-Bot-Api-Secret-Token`, constrain request size, and accept only supported update types.
2. Insert the incoming `update_id` into an inbox table with a unique key. Persist before acknowledging with 2xx, so duplicate deliveries are harmless and transient database failure causes Telegram retry.
3. Process inbox rows in a bounded Worker Cron drain (for example, every minute): atomically claim rows using a Postgres RPC/lease, then run the model and tools. Do not rely on post-response `waitUntil` as a durable queue; Cloudflare documents that it can continue for at most 30 seconds.
4. Persist tool invocation keys and outgoing messages/outbox state before delivery; retry with bounded attempts and explicit failed state. A unique update/tool-call key prevents repeating a subscription mutation when work is retried.
5. Keep webhook response short. `waitUntil` may be used for noncritical short telemetry only. Telegram will retry webhook requests after non-2xx responses; its pending update retention is bounded, so inbox processing and operational error visibility matter.

Telegram docs: [setWebhook and secret token](https://core.telegram.org/bots/api#setwebhook), [Update/update_id](https://core.telegram.org/bots/api#update).

## 3. Proposed directory structure

```text
AGENTS.md
apps/
  telegram-worker/
    src/
      index.ts                 # fetch webhook + scheduled inbox drain
      telegram/{webhook,client,schemas}.ts
      agent/{runner,prompt,tools,schemas}.ts
      db/{client,queries}.ts
      domain/{dates,criteria}.ts
    test/                       # Vitest, mocked DB/LLM/Telegram
    package.json
    tsconfig.json
    wrangler.jsonc
    vitest.config.ts
src/tenniswatch/                # retain existing Python collector and monitor
alembic/versions/               # additive DB migrations
tests/                          # existing Python tests, extend offline
.github/workflows/
  watch.yml                     # availability monitor
  policies.yml                  # verified opening schedule monitor
  worker-tests.yml              # optional Node/Vitest CI
docs/phase1-architecture.md
```

## 4. Reuse and migration plan

- Reuse `BaseCollector`, `CollectorRegistry`, the Gangseo adapter, normalized slot observations, state transition logic, event creation, notification dispatch, DB advisory locking, and collection run recording.
- Keep the existing per-facility `Subscription` working while adding a parent natural-language `UserWatch` (owner, watch type, normalized criteria, enabled/expiry) plus a join table to matched facilities. Migrate gradually; do not rewrite existing data access in one step.
- Add `telegram_user_id BIGINT UNIQUE` to Telegram users, separate from destination `chat_id`; authenticate ownership from the inbound Telegram sender.
- Add `telegram_updates` inbox keyed by `update_id`, `agent_tool_invocations` unique on `(update_id, tool_call_id)`, and an outbound message outbox with delivery attempts/lease/last error. These tables make webhook and side-effect retries idempotent.
- Add `conversation_contexts` keyed by Telegram user/chat with `last_intent`, normalized `last_search_filters`, `active_subscription_id`, `updated_at`, and `expires_at`. Use a configurable context TTL (initial default 24 hours); ask again when expired or ambiguous.
- Add normalized region/district metadata and watch date/weekday criteria. Existing subscriptions currently model weekdays and time range but not one-off local date or region.
- Extend policy records with rule type/data, source URL, observed/effective timestamps, verification status, and policy change history. Schedule generation must only emit confirmed notices from verified policy data.
- Keep `AvailabilitySlot.observed_at` as the source timestamp. Compute staleness against a facility/site polling interval, return `last_checked_at` and `is_stale`, and never turn collection errors into unavailable observations.
- Use additive Alembic migrations and database constraints. Existing RLS is enabled; any Worker access must use narrowly controlled server-side credentials and explicit owner filters or narrowly scoped RPCs. Do not expose the service-role key to clients.

## 5. AI tool contract

The model receives typed tool definitions, never SQL. The authenticated `telegram_user_id` is captured by the Worker and injected into tool implementation context; it is not part of model-controlled arguments.

| Tool | Validated input | Result/side effect |
|---|---|---|
| `search_facilities` | region and/or facility query | Supported matching facilities and explicit support status |
| `search_availability` | facility/region, local date or date range, local start/end time | Available slots with facility/court, booking URL, `last_checked_at`, stale flag |
| `get_opening_schedule` | facility and target date | Resident/general opening times, policy verification/source, or unknown |
| `create_subscription` | normalized region/facility criteria, date/weekday, time window, watch type | Insert for authenticated owner; confirm only after commit/read-back |
| `update_subscription` | owned subscription ID and validated patch | Update only if owner matches; confirm DB result |
| `delete_subscription` | owned subscription ID | Soft-disable only after ownership and write confirmation |
| `list_subscriptions` | no user identity argument | Active watches for authenticated owner |

Use Zod (or equivalent) schemas, strict date/time validation, a small maximum number of tool rounds, bounded LLM output/token settings, and per-user call limits. Interpret relative dates in `Asia/Seoul`, distinguish one-off date from recurring weekday, and treat any scraped text as untrusted data. The selected LLM provider remains a deployment decision; do not claim $0 LLM usage until a provider and current quota are verified. Record calls/tokens and enforce a daily cap.

## 6. Worker ↔ Python ↔ Supabase boundary

- Worker uses Supabase HTTPS/PostgREST or explicit RPCs; do not open a direct PostgreSQL connection from the Worker. Keep service credentials in Worker secrets and perform ownership checks in server-side query/RPC logic.
- Python Actions use `DATABASE_URL` with SQLAlchemy/psycopg and Alembic. They write observation timestamps, availability transitions, events, and notification outbox rows transactionally.
- The monitor claims due notifications with row locks/leases, sends Telegram messages, then records success/failure. Constraint keys identify a slot transition event and recipient notification so reruns do not fan out duplicate alerts.
- Worker and collector share the same normalized facility/site identifiers. Unsupported facilities return an explicit unsupported result; a fresh search with no rows is distinguishable from stale or failed collection.

## 7. Free-tier deployment plan and limits

- Cloudflare Worker: free plan is suitable for low-volume webhook and tool requests, but the free CPU budget is 10 ms per request and `waitUntil` has a 30-second post-response ceiling. Keep CPU work small; LLM/network wait is I/O, but webhook work should still be durable through inbox + Cron drain. Verify current plan limits before launch: [Workers limits](https://developers.cloudflare.com/workers/platform/limits/), [Workers execution context](https://developers.cloudflare.com/workers/runtime-apis/context/).
- Supabase: current Free plan docs list 500 MB database size per project and project pausing after one week of inactivity. Availability polling should keep activity regular, but configure monitoring and document resume steps. Confirm current plan details before deployment: [billing](https://supabase.com/docs/guides/platform/billing-on-supabase), [pricing](https://supabase.com/pricing).
- GitHub Actions: GitHub Free currently includes 2,000 minutes/month for standard private-repository runners; standard runners are free for public repositories. An hourly job implies up to about 730 runs/month, so measure setup/browser costs and avoid installing Chromium unless an active adapter needs it. [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
- Telegram Bot API: no paid server is required for ordinary bot messaging; rate-limit outbound notifications and persist retries.
- LLM API: model-provider billing/free quotas are separate and can break the strict $0 target. Use a replaceable provider interface, usage counters, caps, and a no-LLM local/test mode. Treat $0/month as an operating goal, not a guaranteed cost.
- Required secrets: Worker `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `LLM_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`; GitHub Actions `DATABASE_URL`, `TELEGRAM_BOT_TOKEN` and any required destination configuration. Never put secrets in `wrangler.jsonc`, workflow YAML, or committed `.env`.

## 8. Implementation order

1. Phase 1 (this document): inventory, architecture, extension plan, and repo instructions.
2. Phase 2: Worker scaffold; webhook secret verification, Telegram client, durable update inbox/deduplication; Vitest.
3. Phase 3: provider interface, bounded tool calling and input validation; mocked tests.
4. Phase 4: additive Alembic schema, ownership-safe subscription CRUD, conversation TTL and tool idempotency.
5. Phase 5: collector normalization and supported-site verification; add adapters only after actual site/API analysis.
6. Phase 6: availability and verified policy notification workflows, transactional outbox and retries.
7. Phase 7: deployment guide, secrets/webhook setup, operational limits, end-to-end checks, and cost review.

## Phase 1 verification

- `.venv/bin/pytest -q`: 17 passed.
- `.venv/bin/python -m compileall -q src tests alembic`: passed.
- No Worker code or live site integration test was added in Phase 1.

## Phase 3 verification

- `npm test` in `apps/telegram-worker`: 13 passed (mock LLM and tool handlers).
- `npm run typecheck` in `apps/telegram-worker`: passed.
- `.venv/bin/pytest -q`: 17 passed; Python compile check passed.
- No real LLM API request was made in Phase 3; database-backed tools were added and verified separately in Phase 4.

## Phase 4 verification

- `npm test` in `apps/telegram-worker`: 21 passed with mocked Supabase responses.
- `npm run typecheck`: passed.
- `.venv/bin/pytest -q`: 17 passed; Python compile check passed.
- Alembic revisions through `0004` generate offline PostgreSQL SQL. No live Supabase migration or LLM/Telegram request was made in this phase.

## Phase 5 검증

- Python: 34개 통과. 구독 대상 중복 제거, 한국 날짜, 수집 주기, 실패 시 보존, 날짜별 원자적 저장, 오래된 관측 무시, robots 규칙과 페이지 문맥 검증을 포함합니다.
- Worker: 22개 통과, TypeScript 형식 검사 통과. 관측 이후의 수집 실패를 현재 잔여석으로 확정 안내하지 않는 동작을 검증했습니다.
- `0005`까지 PostgreSQL 마이그레이션 SQL 생성 및 Python 문법 검사가 통과했습니다. 실제 Supabase에는 적용하지 않았습니다.
- 실제 점검 명령은 `ROBOTS_UNAVAILABLE`로 중단됐습니다. 실제 시간표 수집·DB 저장·Telegram 발송의 전체 연동 성공으로 보고하지 않습니다.
- 현장 확인과 재현 절차: [강서 수집 점검 기록](gangseo-collector.md).


## Phase 6 검증

- Python 59개, Worker 25개 테스트 및 형식·문법 검사가 통과했습니다.
- `0006`까지 오프라인 PostgreSQL SQL 생성과 두 Workflow의 YAML 구문 검사가 통과했습니다.
- 첫 가용 관측, 반복 관측 억제, 재개방, 신규 구독자, 구독 해제, 신선도 보류, 재시도와 작업 소유권, 오픈 알림 창, 정책 변경과 재검증을 모의 환경에서 확인했습니다.
- 실제 PostgreSQL 동시 실행과 GitHub/Telegram 수신은 검증하지 않았습니다. [운영 절차](monitor-operations.md)를 참고하세요.
