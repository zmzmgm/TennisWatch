import {afterEach,describe,it,expect,vi} from 'vitest';
import worker from '../src/index';
import {issueDashboardToken,verifyDashboardToken} from '../src/dashboard/auth';
import {dashboardRoute} from '../src/dashboard/routes';
import {DASHBOARD_PAGE} from '../../dashboard/worker/index.js';
import type {Env} from '../src/types';
const env:Env={TELEGRAM_BOT_TOKEN:'test-bot-key',TELEGRAM_WEBHOOK_SECRET:'test-dashboard-secret-long',SUPABASE_URL:'https://db.example.test',SUPABASE_SERVICE_ROLE_KEY:'test-private-role',ALLOWED_TELEGRAM_CHAT_IDS:'123',LLM_API_KEY:'test-key',LLM_MODEL:'test-model',DASHBOARD_SITE_ORIGIN:'https://dashboard.example.test'};
afterEach(()=>{vi.unstubAllGlobals();vi.restoreAllMocks()});
const headers=(token:string)=>({Authorization:'Bearer '+token,Origin:'https://dashboard.example.test'});
describe('dashboard authentication',()=>{
 it('expires tokens and rejects tampering or a wrong signing key',async()=>{
  const at=Date.now(),token=await issueDashboardToken(123,123,env.TELEGRAM_WEBHOOK_SECRET,at);
  expect(await verifyDashboardToken(token,env.TELEGRAM_WEBHOOK_SECRET,at+899000)).toMatchObject({user:123,scope:'dashboard:read'});
  expect(await verifyDashboardToken(token,env.TELEGRAM_WEBHOOK_SECRET,at+900000)).toBeNull();
  expect(await verifyDashboardToken(token+'x',env.TELEGRAM_WEBHOOK_SECRET,at)).toBeNull();
  expect(await verifyDashboardToken(token,'different-secret-long',at)).toBeNull();
  expect(await verifyDashboardToken(token,env.TELEGRAM_WEBHOOK_SECRET,at-60000)).toBeNull();
 });
 it('rejects group or forged identities',async()=>{await expect(issueDashboardToken(123,999,env.TELEGRAM_WEBHOOK_SECRET)).rejects.toThrow('INVALID_PRIVATE_IDENTITY')});
 it.each(['','bad.token','x'.repeat(3000)])('rejects malformed input',async token=>{expect(await verifyDashboardToken(token,env.TELEGRAM_WEBHOOK_SECRET)).toBeNull()});
 it('serves a data-free login page and rejects unauthenticated data access',async()=>{
  const fetcher=vi.fn();vi.stubGlobal('fetch',fetcher);
  const page=await worker.fetch(new Request('https://worker.test/dashboard'),env);
  expect(page.status).toBe(200);expect(await page.text()).toContain('Telegram 접속 코드');
  expect((await worker.fetch(new Request('https://worker.test/api/dashboard'),env)).status).toBe(401);
  expect(fetcher).not.toHaveBeenCalled();expect(page.headers.get('Cache-Control')).toBe('no-store');
  expect(page.headers.get('Content-Security-Policy')).toContain("frame-ancestors 'none'");
 });
 it('checks origin and allowlist before DB access',async()=>{
  const fetcher=vi.fn();vi.stubGlobal('fetch',fetcher);
  const other=await issueDashboardToken(999,999,env.TELEGRAM_WEBHOOK_SECRET);
  expect((await dashboardRoute(new Request('https://worker.test/api/dashboard',{headers:headers(other)}),env)).status).toBe(403);
  const own=await issueDashboardToken(123,123,env.TELEGRAM_WEBHOOK_SECRET);
  expect((await dashboardRoute(new Request('https://worker.test/api/dashboard',{headers:{...headers(own),Origin:'https://untrusted.example'}}),env)).status).toBe(403);
  expect(fetcher).not.toHaveBeenCalled();
 });
 it('rejects mutations and caller-selected ownership',async()=>{
  const token=await issueDashboardToken(123,123,env.TELEGRAM_WEBHOOK_SECRET);
  expect((await dashboardRoute(new Request('https://worker.test/api/dashboard',{method:'POST',headers:headers(token)}),env)).status).toBe(405);
  expect((await dashboardRoute(new Request('https://worker.test/api/dashboard?user_id=999',{headers:headers(token)}),env)).status).toBe(400);
 });
});
describe('dashboard owned data',()=>{
 it('scopes private tables and facility histories to the authenticated owner',async()=>{
  const token=await issueDashboardToken(123,123,env.TELEGRAM_WEBHOOK_SECRET);
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL)=>{
   const url=new URL(String(input)),path=url.pathname;
   if(path.endsWith('/telegram_users')){expect(url.searchParams.get('telegram_user_id')).toBe('eq.123');expect(url.searchParams.get('chat_id')).toBe('eq.123');return Response.json([{id:17,enabled:true}])}
   if(path.endsWith('/facilities')){expect(url.searchParams.get('id')).toBe('in.(7)');return Response.json([{id:7,name:'모의 코트'}])}
   if(path.endsWith('/collection_runs')){expect(url.searchParams.get('facility_id')).toBe('in.(7)');return Response.json([{facility_id:7,status:'ERROR',error_code:'private database content'}])}
   expect(url.searchParams.get('user_id')).toBe('eq.17');
   if(path.endsWith('/user_watches'))return Response.json([{id:1,criteria:{},enabled:true,user_watch_facilities:[{facility_id:7}]}]);
   if(path.endsWith('/daily_report_settings'))return Response.json([{enabled:true,minute_of_day:540}]);
   if(path.endsWith('/notifications'))return Response.json([{kind:'daily_report',status:'SENT',last_error:null}]);
   return Response.json([]);
  }));
  const response=await worker.fetch(new Request('https://worker.test/api/dashboard',{headers:headers(token)}),env);
  expect(response.status).toBe(200);const json=await response.json() as {data:{daily_report:{minute_of_day:number}}};
  expect(json.data.daily_report.minute_of_day).toBe(540);expect(JSON.stringify(json)).not.toContain('private');expect(JSON.stringify(json)).not.toContain(token);
  expect(response.headers.get('Access-Control-Allow-Origin')).toBe('https://dashboard.example.test');
 });
 it('rejects disabled users and hides database errors',async()=>{
  const token=await issueDashboardToken(123,123,env.TELEGRAM_WEBHOOK_SECRET);
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(Response.json([{id:17,enabled:false}])));
  expect((await worker.fetch(new Request('https://worker.test/api/dashboard',{headers:headers(token)}),env)).status).toBe(403);
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue(Response.json({message:'private database error'},{status:500})));
  const response=await worker.fetch(new Request('https://worker.test/api/dashboard',{headers:headers(token)}),env);
  expect(response.status).toBe(503);expect(await response.text()).not.toContain('private');
 });
 it('issues codes through the bot without contacting an LLM',async()=>{
  let sent='';const calls:string[]=[];
  vi.stubGlobal('fetch',vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{
   const url=new URL(String(input));calls.push(url.hostname);
   if(url.pathname.endsWith('/claim_telegram_updates'))return Response.json([{update_id:55,telegram_user_id:123,chat_id:123,message_text:'/dashboard',attempts:1,lease_token:'lease'}]);
   if(url.pathname.endsWith('/telegram_users'))return Response.json([{id:17,chat_id:'123',telegram_user_id:123}]);
   if(url.pathname.endsWith('/conversation_contexts'))return Response.json([]);
   if(url.pathname.endsWith('/telegram_updates'))return Response.json([{update_id:55}]);
   if(url.pathname.endsWith('/sendMessage')){sent=JSON.parse(String(init?.body)).text;return Response.json({ok:true})}
   throw new Error('UNEXPECTED_REQUEST');
  }));
  await worker.scheduled({scheduledTime:Date.now(),cron:'* * * * *'},env,{waitUntil:vi.fn()});
  expect(await verifyDashboardToken(sent.split('\n').at(-1)!,env.TELEGRAM_WEBHOOK_SECRET)).toMatchObject({user:123});
  expect(sent).toContain('/dashboard');expect(sent).toContain('15분');expect(calls.every(host=>['db.example.test','api.telegram.org'].includes(host))).toBe(true);
 });
 it('has syntactically valid UI scripts with no persistent tokens or HTML insertion',()=>{
  const source=DASHBOARD_PAGE.match(/<script>([\s\S]*)<\/script>/)?.[1];expect(source).toBeTruthy();expect(()=>new Function(source!)).not.toThrow();
  expect(source).not.toMatch(/localStorage|sessionStorage|innerHTML/);expect(DASHBOARD_PAGE).not.toContain(env.SUPABASE_SERVICE_ROLE_KEY);
 });
});
