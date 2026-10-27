import type { Env, ScheduledController } from './types';
import { TelegramBotClient } from './telegram/client';

type Command = 'watch-slots' | 'daily-reports' | 'check-policies';
type Job = { job_key: string; command: string; status: string; run_id: string | null };

async function db(env: Env, path: string, body?: unknown, method = 'POST'): Promise<unknown> {
  const response = await fetch(`${env.SUPABASE_URL.replace(/\/$/, '')}/rest/v1/${path}`, {
    method, signal: AbortSignal.timeout(8000), headers: {
      apikey: env.SUPABASE_SERVICE_ROLE_KEY, Authorization: `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`,
      'Content-Type': 'application/json',
    }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) throw new Error('SCHEDULER_DB_FAILED');
  return response.status === 204 ? null : response.json();
}

export async function schedulerStatus(env: Env): Promise<unknown> {
  return db(env, 'scheduler_jobs?select=job_key,command,status,requested_at,started_at,finished_at,run_id,error_code,summary&order=requested_at.desc&limit=30', undefined, 'GET');
}

export async function dispatch(env: Env, command: Command, key: string): Promise<void> {
  if (await db(env, 'rpc/reserve_scheduler_job', { p_key: key, p_command: command }) !== true) return;
  try {
    const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/watch.yml/dispatches`, {
      method: 'POST', signal: AbortSignal.timeout(10000), headers: {
        Authorization: `Bearer ${env.GITHUB_ACTIONS_TOKEN}`, Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'TennisWatch-Worker', 'Content-Type': 'application/json',
      }, body: JSON.stringify({ ref: env.GITHUB_REF ?? 'main', inputs: { command, job_key: key } }),
    });
    // A network timeout is ambiguous: leave PENDING so an accepted runner can claim it.
    // Definitive API rejection can be marked failed immediately.
    if (!response.ok) await db(env, `scheduler_jobs?job_key=eq.${encodeURIComponent(key)}&status=eq.PENDING`,
      { status: 'FAILED', error_code: `DISPATCH_HTTP_${response.status}`, finished_at: new Date().toISOString() }, 'PATCH');
  } catch {
    console.error('SCHEDULER_DISPATCH_UNCONFIRMED');
  }
}

async function incidents(env: Env): Promise<void> {
  const jobs = await db(env, 'rpc/claim_scheduler_incidents', {}) as Job[];
  for (const job of jobs) {
    const link = job.run_id && /^\d+$/.test(job.run_id)
      ? `https://github.com/${env.GITHUB_REPOSITORY}/actions/runs/${job.run_id}`
      : `https://github.com/${env.GITHUB_REPOSITORY}/actions`;
    try {
      await new TelegramBotClient(env.TELEGRAM_BOT_TOKEN).sendMessage(Number(env.SCHEDULER_ALERT_CHAT_ID),
        `⚠️ TennisWatch ${job.command}: ${job.status === 'FAILED' ? '실패' : '45분 이내 완료 확인 없음'}\n실행 키: ${job.job_key}\n${link}\n수집·발송 기록과 데이터 신선도를 확인해 주세요.`);
      await db(env, `scheduler_jobs?job_key=eq.${encodeURIComponent(job.job_key)}`, { alerted_at: new Date().toISOString() }, 'PATCH');
    } catch { console.error('SCHEDULER_ALERT_FAILED'); }
  }
}

export async function schedule(controller: ScheduledController, env: Env): Promise<void> {
  if (env.SCHEDULER_ENABLED !== 'true') return;
  if (!env.GITHUB_ACTIONS_TOKEN || !/^[\w.-]+\/[\w.-]+$/.test(env.GITHUB_REPOSITORY ?? '') ||
      !/^-?\d+$/.test(env.SCHEDULER_ALERT_CHAT_ID ?? '')) throw new Error('SCHEDULER_CONFIG_INVALID');
  const time = new Date(controller.scheduledTime);
  const stamp = time.toISOString();
  try {
    if (controller.cron === '7 * * * *') await dispatch(env, 'watch-slots', `watch:${stamp.slice(0, 13)}`);
    if (controller.cron === '17 * * * *') await dispatch(env, 'check-policies', `policies:${stamp.slice(0, 13)}`);
    // Dedicated 09:00 KST trigger. The minute handler preserves custom report times
    // and catches delayed dispatches; both paths use the same five-minute key.
    if (controller.cron === '0 0 * * *' || (controller.cron === '* * * * *' && time.getUTCMinutes() % 5 === 0)) {
      if (await db(env, 'rpc/daily_reports_due', {}) === true) {
        time.setUTCMinutes(Math.floor(time.getUTCMinutes() / 5) * 5, 0, 0);
        await dispatch(env, 'daily-reports', `daily:${time.toISOString().slice(0, 16)}`);
      }
      await incidents(env);
    }
  } catch {
    console.error('SCHEDULER_CHECK_FAILED');
    // DB outages cannot use DB deduplication. Limit fallback to the hourly trigger.
    if (controller.cron === '7 * * * *') {
      await new TelegramBotClient(env.TELEGRAM_BOT_TOKEN).sendMessage(Number(env.SCHEDULER_ALERT_CHAT_ID),
        '⚠️ TennisWatch 스케줄러 상태 저장/확인 실패. Worker 로그와 Supabase 연결을 확인해 주세요.');
    }
    throw new Error('SCHEDULER_CHECK_FAILED');
  }
}
