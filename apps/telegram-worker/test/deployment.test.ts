import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import { LlmRequestError } from "../src/agent/llm";
import { BudgetedLlmClient, BudgetExceededError, SupabaseBudgetStore } from "../src/agent/budget";
import { SupabaseInboxRepository } from "../src/db/supabase-inbox";
import { handleWebhook, processInboxBatch } from "../src/telegram/webhook";
import type { Env, RuntimeDeps } from "../src/types";

const env: Env = {
  TELEGRAM_BOT_TOKEN: "test-token", TELEGRAM_WEBHOOK_SECRET: "test-webhook-secret-123",
  SUPABASE_URL: "https://db.example.test", SUPABASE_SERVICE_ROLE_KEY: "test-role-key",
  LLM_API_KEY: "test-llm-key", LLM_MODEL: "test-model", ALLOWED_TELEGRAM_CHAT_IDS: "123",
};
const message = { update_id: 5, telegram_user_id: 123, chat_id: 123, message_text: "안녕", attempts: 1, lease_token: "lease-5" };

function runtime(): RuntimeDeps {
  return { webhookSecret: env.TELEGRAM_WEBHOOK_SECRET, inboxBatchSize: 1,
    repo: { enqueue: vi.fn().mockResolvedValue("inserted"), claim: vi.fn().mockResolvedValue([message]),
      storeReply: vi.fn().mockResolvedValue(undefined), markSent: vi.fn().mockResolvedValue(undefined), markFailure: vi.fn() },
    telegram: { sendMessage: vi.fn().mockResolvedValue(undefined) },
    assistant: { respond: vi.fn().mockResolvedValue({ text: "확인했어요." }) }, allowedChatIds: new Set([123]),
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("deployment safeguards", () => {
  it("ignores non-owner messages before database persistence", async () => {
    const deps = runtime();
    const req = new Request("https://worker.test/telegram/webhook", { method: "POST",
      headers: { "X-Telegram-Bot-Api-Secret-Token": env.TELEGRAM_WEBHOOK_SECRET },
      body: JSON.stringify({ update_id: 1, message: { text: "hello", from: { id: 999, is_bot: false }, chat: { id: 999, type: "private" } } }),
    });
    expect((await handleWebhook(req, deps)).status).toBe(200);
    expect(deps.repo.enqueue).not.toHaveBeenCalled();
  });

  it("reuses a persisted reply on retry instead of calling the model again", async () => {
    const deps = runtime();
    vi.mocked(deps.repo.claim).mockResolvedValue([{ ...message, response_text: "저장된 응답" }]);
    expect(await processInboxBatch(deps)).toEqual({ processed: 1, failed: 0 });
    expect(deps.assistant!.respond).not.toHaveBeenCalled();
    expect(deps.telegram.sendMessage).toHaveBeenCalledWith(123, "저장된 응답");
    expect(deps.repo.markSent).toHaveBeenCalledWith(5, "lease-5");
  });

  it("returns a cached limit notice without retrying paid requests", async () => {
    const deps = runtime();
    vi.mocked(deps.assistant!.respond).mockRejectedValue(new BudgetExceededError());
    expect(await processInboxBatch(deps)).toEqual({ processed: 1, failed: 0 });
    expect(deps.repo.storeReply).toHaveBeenCalledWith(5, "lease-5", expect.stringContaining("한도"));
    expect(deps.repo.markFailure).not.toHaveBeenCalled();
  });

  it("requires the secret and a working privileged DB readiness check", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json(true));
    vi.stubGlobal("fetch", fetcher);
    expect((await worker.fetch(new Request("https://worker.test/ready"), env)).status).toBe(401);
    expect(fetcher).not.toHaveBeenCalled();
    const headers = { "X-Telegram-Bot-Api-Secret-Token": env.TELEGRAM_WEBHOOK_SECRET };
    expect((await worker.fetch(new Request("https://worker.test/ready", { headers }), env)).status).toBe(200);
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("worker_readiness"), expect.any(Object));
  });

  it("fences inbox updates with the claim token", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      expect(new URL(String(input)).searchParams.get("lease_token")).toBe("eq.lease-5");
      return Response.json([]);
    });
    vi.stubGlobal("fetch", fetcher);
    await expect(new SupabaseInboxRepository(env).markSent(5, "lease-5")).rejects.toThrow("INBOX_LEASE_LOST");
  });
});

describe("persistent model budget", () => {
  it("does not call a model when the DB refuses a reservation", async () => {
    const complete = vi.fn();
    const store = { reserve: vi.fn().mockRejectedValue(new BudgetExceededError()), finish: vi.fn() };
    await expect(new BudgetedLlmClient({ complete }, store).complete([], [])).rejects.toBeInstanceOf(BudgetExceededError);
    expect(complete).not.toHaveBeenCalled();
  });

  it("records actual usage after the request and keeps failed attempts accounted for", async () => {
    const store = { reserve: vi.fn().mockResolvedValue(1), finish: vi.fn().mockResolvedValue(undefined) };
    const complete = vi.fn().mockResolvedValue({ message: { role: "assistant", content: "안녕" }, usage: { inputTokens: 20, outputTokens: 4 } });
    await new BudgetedLlmClient({ complete }, store).complete([{ role: "user", content: "hello" }], []);
    expect(store.finish).toHaveBeenCalledWith(1, expect.any(String), { inputTokens: 20, outputTokens: 4 });
    complete.mockRejectedValueOnce(new Error("sensitive provider detail"));
    await expect(new BudgetedLlmClient({ complete }, store).complete([], [])).rejects.toThrow("LLM_REQUEST_FAILED");
    expect(store.finish).toHaveBeenLastCalledWith(1, expect.any(String), undefined, true);
  });

  it("sends authenticated update identity and configured limits to the reservation RPC", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init.body));
      expect(body).toMatchObject({ p_user_id: 123, p_update_id: 5, p_user_limit: 20, p_global_limit: 100, p_token_budget: 100000 });
      return Response.json({ ok: false, code: "LLM_BUDGET_EXCEEDED" });
    }));
    await expect(new SupabaseBudgetStore(env, 123, 5).reserve("request-1", 1000)).rejects.toBeInstanceOf(BudgetExceededError);
  });
});

it("records context initialization failure without leaking provider messages or calling the model", async () => {
  const deps = runtime();
  deps.conversations = { load: vi.fn().mockRejectedValue(new Error('private response body')),
    save: vi.fn() };
  expect(await processInboxBatch(deps)).toEqual({ processed: 0, failed: 1 });
  expect(deps.repo.markFailure).toHaveBeenCalledWith(5, 'lease-5', 'CONTEXT_LOAD_FAILED', 1);
  expect(deps.assistant!.respond).not.toHaveBeenCalled();
});


it("sends a cached quota failure notice rather than retrying permanent model failures", async () => {
  const deps = runtime();
  vi.mocked(deps.assistant!.respond).mockRejectedValue(new LlmRequestError('LLM_QUOTA_EXCEEDED'));
  expect(await processInboxBatch(deps)).toEqual({ processed: 1, failed: 0 });
  expect(deps.repo.storeReply).toHaveBeenCalledWith(5, 'lease-5', expect.stringContaining('크레딧'));
  expect(deps.repo.markFailure).not.toHaveBeenCalled();
});
