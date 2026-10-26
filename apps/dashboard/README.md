# TennisWatch 알림 대시보드

조회 전용 화면의 공용 소스는 `worker/index.js`입니다. 기존 Telegram Worker에서도 같은 화면을 사용합니다.

- 기존 Worker 주소: `https://tenniswatch-telegram-worker.tenniswatch-kkh.workers.dev/dashboard`
- Telegram 봇에 `/dashboard`를 보내고 받은 15분 접속 코드를 입력합니다.
- 코드 발급과 대시보드 조회에서는 LLM을 호출하지 않습니다.
- 감시 조건·매일 보고 시각·최근 수집·발송 기록만 조회하며 수정은 Telegram에서 요청합니다.
- 코드는 브라우저 메모리에만 보관합니다. 만료·연결 해제·페이지 새로고침 후에는 다시 입력합니다.

```bash
npm run build
npm run validate
```

`dist/server/index.js`와 `.openai/hosting.json`이 Sites 게시용 산출물입니다. `.openai/hosting.json`의 프로젝트 ID는 유지합니다. 별도 Sites 프로젝트는 비공개로 생성됐지만, 개발 환경에서 `git.chatgpt-team.site` DNS 조회가 실패해 소스 동기화와 게시를 완료하지 못했습니다. 같은 ID를 재사용하며 사이트를 다시 생성하지 마세요. 게시된 전용 URL이 있다고 주장하지 않습니다.

현재 사용할 배포 경로는 기존 Worker입니다. 프로젝트 루트의 `docs/dashboard.md`를 참고해 Worker를 배포합니다. Supabase 키를 이 디렉터리나 브라우저 코드에 복사하지 않습니다.
