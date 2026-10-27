import { afterEach, describe, expect, it, vi } from 'vitest';
import { dispatch, schedule } from '../src/scheduler';
import type { Env } from '../src/types';

const env: Env = { TELEGRAM_BOT_TOKEN: 'test', TELEGRAM_WEBHOOK_SECRET: 'test', SUPABASE_URL: 'https://db.test',
  SUPABASE_SERVICE_ROLE_KEY: 'test', SCHEDULER_ENABLED: 'true', GITHUB_ACTIONS_TOKEN: 'test',
  GITHUB_REPOSITORY: 'zmzmgm/TennisWatch', SCHEDULER_ALERT_CHAT_ID: '123' };
afterEach(() => vi.unstubAllGlobals());
function mock(duplicate = false, due = false) {
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes('reserve_scheduler_job')) return Response.json(!duplicate);
    if (url.includes('daily_reports_due')) return Response.json(due);
    if (url.includes('claim_scheduler_incidents')) return Response.json([]);
    return new Response(null, { status: 204 });
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

describe('scheduler', () => {
  it('is opt-in for safe migration', async () => {
    const f = mock();
    await schedule({ cron: '7 * * * *', scheduledTime: Date.now() }, { ...env, SCHEDULER_ENABLED: 'false' });
    expect(f).not.toHaveBeenCalled();
  });
  it('dispatches hourly watch with an idempotent UTC hour key', async () => {
    const f = mock();
    await schedule({ cron: '7 * * * *', scheduledTime: Date.parse('2026-10-28T00:07:00Z') }, env);
    const call = f.mock.calls.find(([url]) => url.includes('api.github.com'))!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({ ref: 'main', inputs: { command: 'watch-slots', job_key: 'watch:2026-10-28T00' } });
    expect(call[0]).toContain('/workflows/watch.yml/dispatches');
  });
  it('does not dispatch a duplicate reservation', async () => {
    const f = mock(true);
    await dispatch(env, 'watch-slots', 'one');
    expect(f).toHaveBeenCalledTimes(1);
  });
  it('uses the same daily key for 09:00 KST and minute catch-up', async () => {
    const f = mock(false, true);
    const scheduledTime = Date.parse('2026-10-28T00:00:00Z');
    for (const cron of ['0 0 * * *', '* * * * *']) await schedule({ cron, scheduledTime }, env);
    const bodies = f.mock.calls.filter(([url]) => url.includes('api.github.com')).map(([,init]) => JSON.parse(String(init?.body)));
    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toEqual(bodies[1]);
    expect(bodies[0].inputs.command).toBe('daily-reports');
  });
  it('does not request a report without enabled due settings', async () => {
    const f = mock();
    await schedule({ cron: '0 0 * * *', scheduledTime: Date.now() }, env);
    expect(f.mock.calls.some(([url]) => url.includes('api.github.com'))).toBe(false);
  });
  it('preserves policy reminders on their own hourly trigger', async () => {
    const f = mock();
    await schedule({ cron: '17 * * * *', scheduledTime: Date.now() }, env);
    const call = f.mock.calls.find(([url]) => url.includes('api.github.com'))!;
    expect(JSON.parse(String(call[1]?.body)).inputs.command).toBe('check-policies');
  });
  it('records API rejection without leaking response text', async () => {
    const f = mock();
    f.mockImplementation(async (url) => url.includes('reserve_scheduler_job') ? Response.json(true)
      : url.includes('api.github.com') ? new Response('secret', { status: 403 }) : new Response(null, {status:204}));
    await dispatch(env, 'watch-slots', 'one');
    expect(f.mock.calls[2][1]?.body).toContain('DISPATCH_HTTP_403');
    expect(f.mock.calls[2][1]?.body).not.toContain('secret');
  });
  it('leaves ambiguous dispatch pending for watchdog instead of issuing duplicates', async () => {
    const f = mock();
    f.mockImplementation(async url => {
      if (url.includes('reserve_scheduler_job')) return Response.json(true);
      throw new Error('timeout');
    });
    await dispatch(env, 'watch-slots', 'one');
    expect(f).toHaveBeenCalledTimes(2);
  });
  it('alerts on claimed failures/timeouts and acknowledges only successful sends', async () => {
    const f = mock();
    f.mockImplementation(async url => {
      if (url.includes('daily_reports_due')) return Response.json(false);
      if (url.includes('claim_scheduler_incidents')) return Response.json([{job_key:'one', command:'watch-slots',status:'RUNNING',run_id:'123'}]);
      if (url.includes('api.telegram.org')) return Response.json({ok:true});
      return new Response(null, {status:204});
    });
    await schedule({cron:'* * * * *',scheduledTime:Date.parse('2026-10-28T00:50:00Z')},env);
    const send = f.mock.calls.find(([url]) => url.includes('api.telegram.org'))!;
    expect(send[1]?.body).toContain('45분');
    expect(send[1]?.body).toContain('/actions/runs/123');
    expect(f.mock.calls.at(-1)?.[1]?.body).toContain('alerted_at');
  });
});

it('does not acknowledge a failed incident send', async () => {
  const f = mock();
  f.mockImplementation(async url => {
    if (url.includes('daily_reports_due')) return Response.json(false);
    if (url.includes('claim_scheduler_incidents')) return Response.json([{job_key:'one',command:'watch-slots',status:'FAILED',run_id:null}]);
    if (url.includes('api.telegram.org')) return new Response(null,{status:503});
    throw new Error('unexpected acknowledgement');
  });
  await schedule({cron:'* * * * *',scheduledTime:Date.parse('2026-10-28T00:50:00Z')},env);
  expect(f.mock.calls.some(([,init]) => init?.method === 'PATCH')).toBe(false);
});

it('protects scheduler status with webhook authentication', async () => {
  const worker = (await import('../src/index')).default;
  const f = mock();
  const denied = await worker.fetch(new Request('https://worker.test/scheduler/status'),env);
  expect(denied.status).toBe(401);
  expect(f).not.toHaveBeenCalled();
});
