import type { Env } from '../types';

export async function loadDashboard(env: Env, telegramUser: number, chat: number) {
  const base=env.SUPABASE_URL.replace(/\/$/,'')+'/rest/v1/';
  async function rows(path:string, params:Record<string,string>) {
    const url=new URL(base+path);
    Object.entries(params).forEach(([k,v])=>url.searchParams.set(k,v));
    const response=await fetch(url,{headers:{apikey:env.SUPABASE_SERVICE_ROLE_KEY,
      Authorization:'Bearer '+env.SUPABASE_SERVICE_ROLE_KEY},signal:AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error('DASHBOARD_DB_UNAVAILABLE');
    const body=await response.json() as Record<string,unknown>[];
    if (!Array.isArray(body)) throw new Error('DASHBOARD_DB_UNAVAILABLE');
    return body;
  }
  const users=await rows('telegram_users',{select:'id,enabled',telegram_user_id:'eq.'+telegramUser,chat_id:'eq.'+chat,limit:'1'});
  if (users.length!==1||users[0].enabled!==true||!Number.isSafeInteger(users[0].id)) return null;
  const owner=String(users[0].id);
  const [watches, daily, notifications, legacy]=await Promise.all([
    rows('user_watches',{select:'id,watch_type,criteria,enabled,monitoring_status,expires_at,user_watch_facilities(facility_id)',user_id:'eq.'+owner,order:'id.desc',limit:'100'}),
    rows('daily_report_settings',{select:'minute_of_day,enabled,next_run_at,updated_at',user_id:'eq.'+owner,limit:'1'}),
    rows('notifications',{select:'kind,status,attempts,sent_at,next_attempt_at,last_error,report_date',user_id:'eq.'+owner,order:'id.desc',limit:'15'}),
    rows('subscriptions',{select:'facility_id,weekdays,start_time,end_time,enabled',user_id:'eq.'+owner,limit:'100'}),
  ]);
  const facilityIds=new Set<number>();
  for (const watch of watches) {
    for (const link of (Array.isArray(watch.user_watch_facilities)?watch.user_watch_facilities:[]) as {facility_id?:unknown}[]) {
      if (Number.isSafeInteger(link.facility_id)) facilityIds.add(Number(link.facility_id));
    }
  }
  for (const row of legacy) if (Number.isSafeInteger(row.facility_id)) facilityIds.add(Number(row.facility_id));
  const ids=[...facilityIds].slice(0,100);
  const [facilities,runs]=ids.length?await Promise.all([
    rows('facilities',{select:'id,name,region,booking_url,collection_status,last_collected_at,enabled',id:'in.('+ids.join(',')+')',limit:'100'}),
    rows('collection_runs',{select:'facility_id,status,records_seen,started_at,finished_at,duration_ms,error_code,requested_dates',facility_id:'in.('+ids.join(',')+')',order:'started_at.desc',limit:'15'}),
  ]):[[],[]];
  const safeCode=(x:unknown)=>typeof x==='string'&&/^[A-Z][A-Z_0-9]{0,79}$/.test(x)?x:null;
  const safeRuns=runs.map(row=>({...row,error_code:safeCode(row.error_code)}));
  const safeNotifications=notifications.map(row=>({...row,last_error:safeCode(row.last_error)}));
  return {generated_at:new Date().toISOString(),timezone:'Asia/Seoul',watches,daily_report:daily[0]??null,
    legacy_subscriptions:legacy,facilities,collection_runs:safeRuns,notifications:safeNotifications,
    limits:{watches:100,history:15},execution_note:'정기 작업은 기본 매시 7분이며 GitHub 사정에 따라 지연될 수 있습니다.'};
}
