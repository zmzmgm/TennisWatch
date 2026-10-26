/** Short-lived, read-only tokens issued only through an authenticated private bot chat. */
export const DASHBOARD_TTL_SECONDS = 15*60;
interface Claims { v: 1; scope: 'dashboard:read'; user: number; chat: number; iat: number; exp: number; nonce: string }
const encoder = new TextEncoder();
const encode = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');
function decode(text: string): Uint8Array<ArrayBuffer> {
  if (!/^[A-Za-z0-9_-]+$/.test(text)) throw new Error('INVALID_TOKEN');
  return Uint8Array.from(atob(text.replaceAll('-','+').replaceAll('_','/')+'='.repeat((4-text.length%4)%4)), x=>x.charCodeAt(0));
}
async function key(secret: string) {
  if (secret.length<16) throw new Error('DASHBOARD_NOT_CONFIGURED');
  return crypto.subtle.importKey('raw',encoder.encode('TennisWatch/dashboard/read/v1:'+secret),{name:'HMAC',hash:'SHA-256'},false,['sign','verify']);
}
export async function issueDashboardToken(user: number, chat: number, secret: string, now=Date.now()): Promise<string> {
  if (!Number.isSafeInteger(user)||user<=0||chat!==user) throw new Error('INVALID_PRIVATE_IDENTITY');
  const iat=Math.floor(now/1000);
  const claims:Claims={v:1,scope:'dashboard:read',user,chat,iat,exp:iat+DASHBOARD_TTL_SECONDS,nonce:crypto.randomUUID()};
  const body=encode(encoder.encode(JSON.stringify(claims)));
  const signature=await crypto.subtle.sign('HMAC',await key(secret),encoder.encode(body));
  return body+'.'+encode(new Uint8Array(signature));
}
export async function verifyDashboardToken(token: string, secret: string, now=Date.now()): Promise<Claims|null> {
  if (token.length>2048) return null;
  try {
    const parts=token.split('.');
    if (parts.length!==2||!await crypto.subtle.verify('HMAC',await key(secret),decode(parts[1]),encoder.encode(parts[0]))) return null;
    const c=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(decode(parts[0]))) as Claims;
    const at=Math.floor(now/1000);
    if (c.v!==1||c.scope!=='dashboard:read'||!Number.isSafeInteger(c.user)||c.user<=0||c.chat!==c.user
      ||!Number.isSafeInteger(c.iat)||!Number.isSafeInteger(c.exp)||c.iat>at+30||c.exp<=at||c.exp-c.iat!==DASHBOARD_TTL_SECONDS
      ||typeof c.nonce!=='string'||!/^[a-f0-9-]{36}$/.test(c.nonce)) return null;
    return c;
  } catch { return null; }
}
