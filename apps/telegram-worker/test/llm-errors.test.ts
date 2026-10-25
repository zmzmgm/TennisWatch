import { describe, it, expect, vi } from 'vitest';
import { OpenAICompatibleClient, LlmRequestError } from '../src/agent/llm';
import { BudgetedLlmClient } from '../src/agent/budget';

describe('safe model failures', () => {
  it.each([
    [401, {}, 'LLM_AUTH_FAILED'], [403, {}, 'LLM_ACCESS_DENIED'],
    [404, {}, 'LLM_MODEL_NOT_FOUND'], [400, {}, 'LLM_INVALID_REQUEST'],
    [429, { error: { code: 'insufficient_quota', message: 'private provider text' } }, 'LLM_QUOTA_EXCEEDED'],
    [429, { error: { code: 'rate_limit_exceeded' } }, 'LLM_RATE_LIMITED'],
    [500, {}, 'LLM_PROVIDER_ERROR'],
  ])('records and propagates only a safe code for HTTP %s', async (status, body, code) => {
    const client = new OpenAICompatibleClient({ apiKey: 'private-key', model: 'test',
      fetcher: vi.fn().mockResolvedValue(Response.json(body, { status })) });
    const store = { reserve: vi.fn().mockResolvedValue(1), finish: vi.fn().mockResolvedValue(undefined) };
    await expect(new BudgetedLlmClient(client, store).complete([], [])).rejects.toThrow(String(code));
    expect(store.finish).toHaveBeenCalledWith(1, expect.any(String), undefined, true, code);
    expect(JSON.stringify(store.finish.mock.calls)).not.toContain('private');
  });

  it('sanitizes network exception text', async () => {
    const client = new OpenAICompatibleClient({ apiKey: 'private-key', model: 'test',
      fetcher: vi.fn().mockRejectedValue(new Error('private-url-and-key')) });
    await expect(client.complete([], [])).rejects.toEqual(new LlmRequestError('LLM_NETWORK_FAILED'));
  });
});
