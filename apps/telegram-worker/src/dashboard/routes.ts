import type { Env } from '../types';
import { allowedChats } from '../config';
import { verifyDashboardToken } from './auth';
import { loadDashboard } from './data';
import { DASHBOARD_PAGE } from '../../../dashboard/worker/index.js';

export async function dashboardRoute(request:Request,env:Env):Promise<Response> {
  const url=new URL(request.url);
  const origin=request.headers.get('Origin');
  const accepted=new Set([url.origin,env.DASHBOARD_SITE_ORIGIN].filter(Boolean));
  const headers:Record<string,string>={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','Vary':'Origin'};
  if (origin&&accepted.has(origin)) headers['Access-Control-Allow-Origin']=origin;
  const json=(value:unknown,status=200)=>Response.json(value,{status,headers});
  if (url.pathname==='/dashboard'&&request.method==='GET') {
    return new Response(DASHBOARD_PAGE,{headers:{...headers,'Content-Type':'text/html; charset=utf-8',
      'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self' https://tenniswatch-telegram-worker.tenniswatch-kkh.workers.dev; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"}});
  }
  if (origin&&!accepted.has(origin)) return json({ok:false,code:'ORIGIN_DENIED'},403);
  if (request.method==='OPTIONS') return new Response(null,{status:204,headers:{...headers,'Access-Control-Allow-Methods':'GET, OPTIONS','Access-Control-Allow-Headers':'Authorization','Access-Control-Max-Age':'300'}});
  if (request.method!=='GET') return json({ok:false,code:'READ_ONLY'},405);
  if (url.search) return json({ok:false,code:'INVALID_REQUEST'},400);
  const token=request.headers.get('Authorization')?.match(/^Bearer ([A-Za-z0-9_.-]+)$/)?.[1];
  if (!token) return json({ok:false,code:'LOGIN_REQUIRED'},401);
  const identity=await verifyDashboardToken(token,env.TELEGRAM_WEBHOOK_SECRET);
  if (!identity) return json({ok:false,code:'SESSION_EXPIRED'},401);
  try {
    if (!allowedChats(env).has(identity.chat)) return json({ok:false,code:'ACCESS_DENIED'},403);
    const data=await loadDashboard(env,identity.user,identity.chat);
    return data?json({ok:true,data,session_expires_at:new Date(identity.exp*1000).toISOString()}):json({ok:false,code:'ACCESS_DENIED'},403);
  } catch { return json({ok:false,code:'DATA_UNAVAILABLE'},503); }
}
