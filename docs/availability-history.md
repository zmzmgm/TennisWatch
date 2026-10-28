# 예약 상태 변경 내역 조회

Telegram Agent의 `search_availability_changes` 도구는 DB에 저장된 `availability_events` 중
`UNKNOWN → AVAILABLE` 또는 `UNAVAILABLE → AVAILABLE` 전환만 조회한다. 별도 날짜를 지정하지 않으면 `SUPPORTED` 상태인
전체 시설의 모든 이용 날짜를 대상으로 변경 시각이 최신인 순서대로 반환한다.

- 기본 페이지는 최근 변경 10건이며 한 번에 최대 15건을 반환한다.
- `page`로 이전 기록을 이어서 조회한다. `has_more`가 참이면 다음 페이지가 있다.
- 시설명, 지역, 이용 날짜를 선택 조건으로 지정할 수 있다.
- 결과는 `이용 날짜`와 `변경 시각`을 따로 표시한다.
- `UNKNOWN → AVAILABLE`과 `UNAVAILABLE → AVAILABLE`을 포함한다. 처음 상태를 모를 때나 예약 불가 상태에서 예약 가능으로 바뀐 경우다.
- 반복 관측이 아니라 상태 전이만 저장한다. `AVAILABLE → UNAVAILABLE` 전환은 이 조회에 포함되지 않는다.
- 이력은 과거 기록이며 현재 예약 가능 여부를 뜻하지 않는다. 현재 상태는 날짜별 `search_availability`로 확인한다.
- 실제 수집 전환만 반환한다. 수집 실패는 변경 이벤트를 만들지 않으며, 이전 상태를 그대로 둔다.

예시 요청:

- “최근 수집에서 예약 상태가 바뀐 코트를 알려줘.”
- “우장산의 변경 내역 더 보여줘.”
- “10월 28일 이용분에서 바뀐 상태를 확인해줘.”

Worker 변경을 운영에 적용하려면 `apps/telegram-worker`에서 테스트와 타입 검사를 실행한 뒤
Cloudflare Worker를 다시 배포해야 한다. 배포 전까지 Telegram 봇에서는 새 도구를 사용할 수 없다.
