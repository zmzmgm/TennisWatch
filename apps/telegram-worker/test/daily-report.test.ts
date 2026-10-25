import { afterEach, describe, it, expect, vi } from 'vitest';
import { ToolRegistry } from '../src/agent/tools';
import { AgentRunner } from '../src/agent/runner';
import { SupabaseTennisTools } from '../src/db/tennis-tools';

const context = { telegramUserId: 123, chatId: 123, updateId: 55, toolCallId: 'call-55' };
afterEach(() => vi.unstubAllGlobals());

describe('requested daily snapshots', () => {
  it.each([{local_time:'24:00'}, {local_time:'18:60'}, {}, {local_time:'18:00',user_id:999}, {local_time:'18:00',timezone:'UTC'}])('rejects invalid times and model-controlled ownership', async (input) => {
    const handler = vi.fn();
    expect(await new ToolRegistry({set_daily_report:handler}).execute('set_daily_report',input,context)).toMatchObject({ok:false,code:'invalid_input'});
    expect(handler).not.toHaveBeenCalled();
  });

  it('sends only authenticated identity and validated Korea time to the transactional RPC', async () => {
    const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      expect(String(url)).toContain('/rpc/manage_daily_report');
      expect(JSON.parse(String(init?.body))).toEqual({p_update_id:55,p_user_id:123,p_chat_id:'123',p_action:'set',p_minute:18*60+30});
      return Response.json({ok:true,data:{enabled:true,local_time:'18:30'}});
    });
    vi.stubGlobal('fetch',fetcher);
    const tools = new SupabaseTennisTools({SUPABASE_URL:'https://db.example.test',SUPABASE_SERVICE_ROLE_KEY:'test-key'});
    const result = await new ToolRegistry(tools.handlers()).execute('set_daily_report',{local_time:'18:30'},context);
    expect(result).toMatchObject({ok:true,data:{enabled:true,local_time:'18:30'}});
  });

  it('never reports save success when the DB rejects the change', async () => {
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response('',{status:403})));
    const tools = new SupabaseTennisTools({SUPABASE_URL:'https://db.example.test',SUPABASE_SERVICE_ROLE_KEY:'test-key'});
    expect(await new ToolRegistry(tools.handlers()).execute('set_daily_report',{local_time:'18:30'},context)).toMatchObject({ok:false});
  });

  it('confirms a committed schedule with one model call and does not claim deployment', async () => {
    const complete = vi.fn().mockResolvedValue({message:{role:'assistant',content:null,tool_calls:[{
      id:'call-55',type:'function',function:{name:'set_daily_report',arguments:'{"local_time":"18:30"}'},
    }]}});
    const tools = new ToolRegistry({set_daily_report:vi.fn().mockResolvedValue({ok:true,data:{enabled:true,local_time:'18:30'}})});
    const reply = await new AgentRunner({complete},tools).respond(123,'매일 오후 6시 반 결과 알려줘',55);
    expect(reply.text).toContain('18:30');
    expect(reply.text).toContain('저장');
    expect(reply.text).toContain('정기 작업');
    expect(complete).toHaveBeenCalledTimes(1);
  });

  it('does not claim a stale or unconfirmed request was saved', async () => {
    const complete = vi.fn().mockResolvedValue({message:{role:'assistant',content:null,tool_calls:[{
      id:'call-55',type:'function',function:{name:'set_daily_report',arguments:'{"local_time":"18:30"}'},
    }]}});
    const tools = new ToolRegistry({set_daily_report:vi.fn().mockResolvedValue({ok:false,code:'stale_update'})});
    const reply = await new AgentRunner({complete},tools).respond(123,'매일 오후 6시 반 결과 알려줘',55);
    expect(reply.text).not.toContain('저장했');
  });
});
