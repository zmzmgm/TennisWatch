# Worker 스케줄러 → GitHub Collector 전환

## 실행 구조

Cloudflare Worker는 기존 Telegram AI Agent/웹훅/대시보드를 유지하고 Cron에서 GitHub `workflow_dispatch`를 요청합니다. Python Collector, Supabase, 알림 outbox는 유지합니다. 정기 수집은 LLM을 호출하지 않습니다. `watch.yml`과 `policies.yml`에는 GitHub `schedule`이 없습니다. `verify.yml`의 push/PR 검증은 그대로 유지합니다.

| Worker Cron (UTC) | 한국 시간 / 역할 | 독립 실행 명령 |
|---|---|---|
| `7 * * * *` | 매시간 7분 취소표 감시 | `watch-slots` |
| `0 0 * * *` | 매일 오전 9시 일일 보고 | `daily-reports` |
| `17 * * * *` | 매시간 17분 기존 정책/예약 오픈 알림 | `check-policies` |
| `* * * * *` | Telegram inbox 재시도; 5분마다 보고 누락/사용자 지정 시각과 실행 장애 확인 | 필요 시 `daily-reports` |

09:00은 **실행 요청 시각**입니다. Actions 대기·의존성 설치·수집 시간이 추가되어 정확히 09:00 수신을 보장하지 않습니다. Cron은 UTC 기준이며 변경 전파에도 시간이 필요합니다. [Cloudflare 공식 문서](https://developers.cloudflare.com/workers/configuration/cron-triggers/).

일일 보고는 활성화된 `daily_report_settings`의 기한이 된 사용자만 대상으로 기존 감시 조건의 오늘부터 7일을 새로 수집하고, 이후 보고를 생성합니다. 기존 수집 쿨다운은 이 작업에서만 우회합니다. 다른 사용자/미등록 코트를 전수 수집하지 않습니다. 사용자가 지정한 다른 보고 시각도 5분 단위 점검으로 보존합니다. 시간 설정을 자동으로 덮어쓰거나 보고를 임의 활성화하지 않습니다.

시간당 감시는 활성·첫 확인 대기 감시와 기존 구독을 유지합니다. 최근 30분 내 성공 수집은 재사용하여 09시 보고와 09:07 감시의 불필요한 중복을 줄입니다. 60분 쿨다운을 그대로 적용하면 Actions 시작 지연의 차이 때문에 다음 시간 수집이 빠질 수 있어 시간당 실행의 최소 재수집 간격은 30분입니다. 사이트별 요청 간격 5초 이상은 그대로입니다.

## 중복·실패·신선도

- `0010`의 `scheduler_jobs`가 실행 요청과 완료 영수증입니다. `PENDING`은 GitHub 완료가 아닙니다. 동일 실행 키는 DB PK로 한 번만 예약하고, 동일 명령의 45분 내 진행 중 요청도 겹치지 않습니다.
- Runner는 `PENDING → RUNNING` 원자적 갱신을 획득한 경우만 실행합니다. GitHub 재실행/중복 요청은 기존 작업을 반복하지 않습니다. `FAILED` 작업을 복구할 때는 새 수동 실행으로 새 키를 만듭니다.
- GitHub concurrency와 기존 PostgreSQL advisory lock으로 전체 수집 작업을 직렬화합니다. 잠금 연결은 작업 동안 같은 물리 연결에 고정합니다. 잠금 충돌을 성공으로 기록하지 않습니다.
- 성공/실패, 시작·종료 시각, GitHub run ID, 수집·관측·발송 건수, 대상 시설 수, 미확인/오래된 날짜 수, 신선도 확인 시각과 마지막 관측 시각을 저장합니다. 대상이 0이면 관측 성공으로 확대 해석하지 않습니다.
- 수집 실패는 이전 관측을 보존하고 오류를 기록합니다. 일일 보고는 부분 실패를 ‘확인 불가’로 표시합니다. 신선도 기준은 기존 `max(30, site.poll_minutes) × 2분`을 유지합니다. 시계가 1분보다 앞선 관측도 신뢰하지 않습니다.
- Worker는 5분마다 실패 및 요청 후 45분 내 완료되지 않은 실행을 확인합니다. 설치 실패, 시작되지 않은 Actions, 취소, 시간 초과, DB 완료 기록 실패도 미완료로 감지합니다. GitHub API 수락을 실행 성공으로 취급하지 않습니다.
- 실패/미완료 알림은 `SCHEDULER_ALERT_CHAT_ID`로 보냅니다. DB에서 알림 임대를 획득한 뒤 전송하고 성공 시 기록합니다. 발송 실패는 5분 뒤 재시도합니다. 전체 DB 연결 장애는 시간당 감시 트리거에서 별도 운영 알림을 시도합니다.
- GitHub API 타임아웃은 요청 수락 여부를 알 수 없어 같은 키를 즉시 다시 발행하지 않습니다. 영수증을 남기고 45분 감시로 확인합니다. 확정 HTTP 거절은 즉시 실패로 기록합니다.
- 기존 잔여석 이벤트·일일 보고 고유 제약과 outbox의 잠금/재시도/발송 직전 소유자·상태·신선도 재검증을 유지합니다. Telegram 전송 직후 DB 기록 전 프로세스가 중단되면 중복 가능성이 남습니다. 외부 전송을 포함한 정확히 한 번 전달을 보장하지 않습니다.
- GitHub concurrency는 대기 중 실행을 새 실행으로 대체할 수 있습니다. 이 경우 해당 요청은 45분 미완료로 감지합니다. 처리량이 부족하면 대상을 줄이거나 실행 한도를 조정해야 합니다.

## 배포 순서

1. 기존 실행이 끝났는지 확인하고, 이 변경을 기본 브랜치 `main`에 반영합니다. 기본 브랜치에 `workflow_dispatch` 파일이 있어야 합니다. 기존 GitHub 예약은 이 시점에 제거되므로 전환 공백을 짧게 유지하세요.
2. 운영 DB 백업 후 기존 배포 환경에서 `alembic upgrade head`로 `0010`을 적용합니다. `scheduler_jobs`는 RLS를 활성화하며 anon/authenticated는 접근할 수 없습니다. RPC는 service_role만 호출 가능합니다. Python DB 사용자는 기존처럼 테이블 작업 권한이 필요합니다. PostgreSQL 세션 advisory lock 때문에 직접 연결 또는 **session pooler**를 사용하고 transaction pooler는 사용하지 마세요.
3. GitHub Actions Secrets에 기존 `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`을 유지합니다. `TELEGRAM_CHAT_ID`는 기존 호환용 선택 값입니다. LLM 키나 Worker용 GitHub 토큰을 Actions에 추가할 필요는 없습니다.
4. GitHub Fine-grained PAT를 생성합니다. 저장소는 **zmzmgm/TennisWatch 한 개**, Repository permissions는 **Actions: Read and write**만 부여합니다(필수 Metadata read는 자동 포함). 만료일과 교체 계획을 정하세요. Contents write나 classic PAT의 광범위한 repo 권한은 필요 없습니다. [GitHub dispatch 권한 문서](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).
5. `apps/telegram-worker`에서 아래 명령의 대화형 입력으로 Worker Secrets를 저장합니다. 토큰 값을 명령 인자·문서·채팅·로그에 넣지 마세요. 기존 Supabase/Telegram/LLM Secrets는 유지합니다.

   ```sh
   npx wrangler secret put GITHUB_ACTIONS_TOKEN
   npx wrangler secret put SCHEDULER_ALERT_CHAT_ID
   ```

6. `wrangler.jsonc`의 `GITHUB_REPOSITORY`, `GITHUB_REF`를 확인합니다. 안전한 전환을 위해 커밋 기본값은 `SCHEDULER_ENABLED=false`입니다. 준비가 끝나면 `true`로 변경하고 `npm run deploy`합니다. 계정의 Cron 목록이 파일의 네 항목과 일치하는지 확인합니다.
7. 오전 9시 보고를 원하는 기존 사용자에게 봇으로 `매일 오전 9시에 기존 감시 조건의 결과를 알려줘`를 요청하거나, 연결한 사용자 환경에서 `tenniswatch set-daily-report --report-time 09:00`을 실행합니다. 이미 설정되어 있으면 다시 만들 필요가 없습니다.
8. Actions의 **Collector execution → Run workflow**에서 `watch-slots`, `daily-reports`, `check-policies`를 각각 필요에 따라 실행합니다. `job_key`는 비워 둡니다. 수동 daily도 due 설정이 있어야 보고합니다. 수동 테스트는 실제 수집·알림을 발생시키므로 활성 조건을 확인하고 실행하세요.
9. 아래 운영 확인을 마친 뒤 정기 운영으로 판단합니다. 코드·단위 테스트 통과만으로 운영 성공을 선언하지 않습니다.

## 운영 확인과 복구

- 인증된 `GET /scheduler/status`는 최근 30건의 영수증과 summary를 반환합니다. 인증 헤더는 기존 `/ready`와 같은 `X-Telegram-Bot-Api-Secret-Token`입니다. 응답은 `no-store`이며 공개 health에는 운영 데이터를 노출하지 않습니다.
- Supabase에서 `scheduler_jobs` 최신 행을 확인하고 `run_id`의 Actions 로그 및 커밋 SHA와 대조하세요. `SUCCESS`라도 `target_count=0`이면 등록 조건을 확인합니다. `last_observed_at`, `stale_dates`, `unverified_dates`와 `collection_runs`, `facilities.last_collected_at`을 함께 확인하세요.
- 오전 9시에는 `daily-reports`의 별도 실행, 당일 사용자당 daily_report 1건, Telegram API 성공 및 실제 수신을 확인합니다. 매시 7분 작업이 별도 일일 보고를 생성하지 않는지 확인합니다.
- 실패 원인 해결 후 새 Run workflow를 사용합니다. 동일 GitHub 실행의 Re-run은 중복 방지 때문에 수집/발송을 반복하지 않습니다. 발송만 복구하려면 새 `send-outbox` 실행을 선택하세요. 이미 전송된 알림 상태를 임의로 초기화하지 마세요.
- 미완료 상태에서 수동 재실행 전 원래 Actions 실행이 정말 종료됐는지 확인합니다. GitHub 토큰 만료/취소 시 Worker Secret을 교체하고 새 작업으로 검증합니다.
- Worker/Cron 전체 중단은 스스로 알릴 수 없습니다. Cloudflare 실행 오류 로그와 외부 uptime 감시를 함께 운영하세요. 운영 알림용 Telegram 자체 장애도 별도 확인이 필요합니다.
- 롤백은 먼저 `SCHEDULER_ENABLED=false` 배포 후 Worker 요청과 진행 중 Actions 종료를 확인합니다. 그 뒤에만 이전 GitHub schedule을 복원합니다. 두 스케줄러를 동시에 활성화하지 마세요. `0010` 기록은 감사/복구용으로 유지할 수 있습니다.
- `scheduler_jobs`는 누적됩니다. 운영 보관 정책에 따라 완료·알림 처리된 오래된 행을 백업/정리하세요. 보관 기간 이전 키 재발행은 중복 방지 범위 밖이므로 금지합니다.

## 검증 범위

로컬 Python/Worker 테스트는 모의 수집·API 응답으로 중복 요청, 보고 분리, 부분 실패, 신선도, 잠금 충돌, 상태 조회와 장애 알림 경로를 확인합니다. 운영 Supabase `0010` 적용, Worker Secret 등록/배포, 실제 GitHub dispatch·예약 사이트 수집·Telegram 수신은 별도 배포 검증이 필요합니다.

```sh
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests alembic
cd apps/telegram-worker
npm test
npm run typecheck
npm run build:check
```
