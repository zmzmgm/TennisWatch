# 강서 어댑터 확인 기록과 운영 방법

확인일: 2026-10-24. 이 문서는 실제로 확인한 사실과 아직 확인하지 못한 동작을 구분합니다.

## 실제 사이트 확인

- [공개 예약 페이지](https://sports.gangseo.seoul.kr/fmcs/28?center=GANGSEO02&facilities_type=T&part=03&place=2&rent_type=1001)가 Chrome에서 열렸습니다.
- `center`, `part`, `place` 선택 필드, `base_date` 날짜 필드, 날짜 링크 `date-YYYYMMDD`, 접근성 이름이 `예약 시간 선택`으로 시작하는 표를 확인했습니다.
- 센터 목록은 로딩 후 `불러오기 오류`로 끝났습니다. 따라서 이번 확인에서는 실제 코트 선택이나 시간대별 잔여석을 검증하지 못했습니다. 사이트 자체 장애인지 현재 접속 환경의 제한인지는 확인되지 않았습니다.
- 브라우저의 `robots.txt` 열기는 클라이언트 차단으로 실패했습니다. 로컬 HTTPX도 DNS 해석에 실패했습니다. 이를 사이트가 모든 수집을 금지했다는 뜻으로 해석하지 않습니다.
- 로컬에 Playwright용 Chromium은 설치되어 있지 않습니다. 실제 점검 명령은 브라우저 시작 전 robots 확인에서 `ROBOTS_UNAVAILABLE`로 중단됐습니다.
- 공식 공개 API는 확인하지 못했습니다. 내부 API 주소를 추측하거나 인증·CAPTCHA를 우회하는 코드는 없습니다.
- [이용약관](https://sports.gangseo.seoul.kr/fmcs/51)과 [우장산 이용안내](https://sports.gangseo.seoul.kr/fmcs/18)는 공개되어 있습니다. 정책 계산에 사용할 정도의 개별 예약 규칙 검증은 아직 완료하지 않았습니다.

## 지원 범위

| 기능 | 상태 |
|---|---|
| 등록된 대상 | 우장산 테니스장 1코트, `GANGSEO02:T:03:2` |
| 시설·코트 자동 발견 | 미지원; 알려진 대상만 명시적으로 등록 |
| 코트 정보 | `init-gangseo`에서 알려진 1코트를 중복 없이 등록 |
| 잔여석 어댑터 | 구현됨; 실제 시간표 수집 성공은 미검증 |
| 예약 정책 자동 수집 | 미지원; 검증된 일정만 기존 DB에서 조회 |
| 다른 강서 시설·전국 수집 | 미지원 |
| 새 감시 조건 자동 알림 | Phase 6 구현됨; 운영 설정 후 첫 확인 성공 시 활성화 |

DB의 `SUPPORTED`는 어댑터가 등록된 대상임을 뜻합니다. 성공적인 실제 수집을 뜻하는 값은 아닙니다. 수집 성공 시에만 `facilities.last_collected_at`을 기록하고, 조회 도구에서 `last_successful_collection_at`으로 제공합니다.

## 실행 순서

프로젝트 루트에서 실행합니다. `.env`에 DB 연결이 이미 설정되어 있어야 합니다.

```bash
.venv/bin/alembic upgrade head
.venv/bin/tenniswatch init-gangseo
.venv/bin/python -m playwright install chromium
```

먼저 DB 기록이나 Telegram 전송 없이 한 날짜를 점검합니다. 아래 날짜는 확인하려는 날짜로 바꿉니다.

```bash
.venv/bin/python -m tenniswatch.cli probe-gangseo --date 2026-10-24
```

성공하면 코트·시간·가용 여부·실제 관측 시각·예약 URL만 JSON으로 출력합니다. HTML이나 예약자·팀 이름은 저장하거나 출력하지 않습니다. 실패하면 안전한 오류 코드와 종료 코드 1을 반환합니다.

그다음 활성 구독이 있는 대상만 DB에 수집합니다.

```bash
.venv/bin/tenniswatch collect-slots
```

이 명령은 Telegram을 발송하지 않습니다. 기존 구독의 상태 전이 이벤트와 알림 대기열은 기존 로직으로 생성할 수 있습니다. 새 `user_watches`도 수집 대상으로 계획합니다. `collect-slots`는 대기 조건을 활성화하지 않으며, `watch-slots`의 첫 확인 성공 후 알림을 활성화합니다. [알림 운영 안내](monitor-operations.md)를 참고하세요.

## 수집 및 저장 규칙

- 한국 날짜 기준으로 오늘부터 `WATCH_DAYS_AHEAD`까지 살펴봅니다(기본 7, 최대 31). 특정 날짜·요일 조건을 반영하고 시설/날짜 중복을 제거합니다.
- 비활성 사용자·시설·사이트·감시 조건과 만료된 감시는 제외합니다. 예약 오픈 전용 조건은 잔여석 수집 대상이 아닙니다.
- 사이트별 `reservation_sites.poll_minutes`를 지킵니다(기본 60, 최소 30). 최근 실패도 주기 판단에 포함해 즉시 재시도로 요청을 반복하지 않습니다.
- `robots.txt`를 먼저 확인합니다. 404/410이면 규칙 없음으로 처리하며, 접근 거부·다른 문서·리다이렉트·서버/연결 오류이면 중단합니다. 금지 경로는 수집하지 않습니다.
- 날짜별 페이지 조회 간격은 최소 5초입니다. robots의 더 긴 간격도 따르며, 60초 초과 지연을 요구하면 이번 실행을 보류합니다.
- 브라우저는 필요할 때 시작하고 같은 사이트의 날짜별 조회에 재사용합니다. 센터·시설·코트 값과 실제 선택된 날짜, 시간표 행 로딩을 확인합니다.
- 동일 날짜의 전체 관측을 검증한 뒤 한 트랜잭션에 저장합니다. 빈 표·잘못된 시간·중복 슬롯·모르는 상태·문맥 불일치는 오류이며 기존 슬롯을 마감으로 바꾸지 않습니다.
- 이전보다 오래됐거나 같은 시각인 관측은 덮어쓰지 않습니다. 관측 시각은 UTC의 timezone-aware 값이며 슬롯 날짜/시각은 `Asia/Seoul`의 현지 날짜와 시각입니다.
- `collection_runs`에 시설, 요청 날짜, 경과 시간, 성공/부분 성공/실패 및 오류 코드를 남깁니다. 예외의 원문이나 사이트 개인정보를 기록하지 않습니다.

## 오류 대응

| 오류 코드 | 확인할 내용 |
|---|---|
| `ROBOTS_UNAVAILABLE` | DNS·네트워크·robots 응답을 확인하고 다음 주기에 재시도 |
| `ROBOTS_DISALLOWED` | 해당 경로 수집 중지; 규칙을 우회하지 않음 |
| `ROBOTS_RATE_LIMIT` | 더 긴 주기/분할 실행 설계 후 재개 |
| `BROWSER_UNAVAILABLE` | `.venv/bin/python -m playwright install chromium` 확인 |
| `ACCESS_RESTRICTED` | 접근 제한 또는 요청 제한; 추가 요청 중지 |
| `PAGE_SELECTION_MISMATCH` | 센터/코트 목록 로딩 오류 또는 식별자 변경 확인 |
| `PAGE_DATE_MISMATCH` | 요청 날짜가 실제 페이지에 적용됐는지 확인 |
| `NO_SLOT_DATA` | 로딩 실패·예약 기간 밖·페이지 구조 변경을 구분해 수동 확인 |
| `UNKNOWN_STATUS`, `INVALID_SLOT`, `DUPLICATE_SLOT` | 실제 표 구조를 확인한 후 파서 수정 |

실패한 시설의 이전 슬롯은 남지만, 조회 도구는 관측 이후의 수집 실패를 인식해 현재 잔여석으로 확정 안내하지 않습니다.

## 검증 범위

Python 테스트의 HTML과 브라우저 응답은 모의 자료입니다. 실제 사이트에서 추출한 슬롯이라고 주장하지 않습니다. SQLite에서 수집 계획, 원자적 저장, 실패 시 상태 보존, 구독 중복 제거, 주기, 오래된 관측 무시를 검증했습니다. Worker 테스트는 Supabase 모의 응답으로 마지막 수집 실패 안내를 검증했습니다. 실제 Supabase 기록·Playwright 시간표 수집·Telegram 전송의 전체 연동은 아직 검증하지 못했습니다.
