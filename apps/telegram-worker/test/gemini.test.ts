import { describe, it, expect, vi } from 'vitest';
import { OpenAICompatibleClient } from '../src/agent/llm';
import { AgentRunner } from '../src/agent/runner';
import { ToolRegistry } from '../src/agent/tools';

describe('Gemini tool protocol', () => {
  it('returns the exact signature on the second call without exposing it to tools or context', async () => {
    const signature = 'opaque-test-signature+/=한글';
    const handler = vi.fn().mockResolvedValue({ ok: true, data: { subscriptions: [] } });
    let round = 0;
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const request = JSON.parse(String(init?.body));
      if (round++ === 0) {
        return Response.json({ choices: [{ message: { content: null, tool_calls: [{
          id: 'gemini-call', type: 'function', function: { name: 'list_subscriptions', arguments: '{}' },
          extra_content: { google: { thought_signature: signature, untrusted: 'discard me' }, untrusted: 'discard me' },
        }] } }] });
      }
      const assistant = request.messages.find((message: {role: string}) => message.role === 'assistant');
      expect(assistant.tool_calls[0].extra_content).toEqual({ google: { thought_signature: signature } });
      expect(request.messages.at(-1)).toMatchObject({ role: 'tool', tool_call_id: 'gemini-call' });
      return Response.json({ choices: [{ message: { content: '등록된 감시 조건이 없어요.' } }] });
    });
    const client = new OpenAICompatibleClient({ apiKey: 'test-key', model: 'test-gemini', fetcher });
    const reply = await new AgentRunner(client, new ToolRegistry({ list_subscriptions: handler }))
      .respond(123, '감시 조건 알려줘', 77);
    expect(reply.text).toContain('감시 조건');
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(JSON.stringify(handler.mock.calls)).not.toContain(signature);
    expect(JSON.stringify(reply)).not.toContain(signature);
  });

  it.each([123, '', 'a'.repeat(65537)])('rejects malformed or oversized signatures', async (signature) => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ choices: [{ message: { tool_calls: [{
      id: 'call', function: { name: 'list_subscriptions', arguments: '{}' },
      extra_content: { google: { thought_signature: signature } },
    }] } }] }));
    await expect(new OpenAICompatibleClient({ apiKey: 'test-key', model: 'test', fetcher }).complete([], []))
      .rejects.toThrow('invalid tool metadata');
  });
});
