import { LlmRequestError } from "../src/agent/llm";
import { describe, expect, it, vi } from "vitest";
import { handleWebhook, processInboxBatch } from "../src/telegram/webhook";
import type { InboxRepository, RuntimeDeps, TelegramTextUpdate } from "../src/types";

const validUpdate: TelegramTextUpdate = {
  update_id: 88,
  message: {
    message_id: 4,
    date: 1_800_000_000,
    from: { id: 123, is_bot: false },
    chat: { id: 123, type: "private" },
    text: "토요일 강서구 찾아줘",
  },
};

function request(update: unknown = validUpdate, secret = "hook-secret"): Request {
  return new Request("https://worker.test/telegram/webhook", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Telegram-Bot-Api-Secret-Token": secret },
    body: JSON.stringify(update),
  });
}

function deps(overrides: Partial<RuntimeDeps> = {}): RuntimeDeps {
  const repo: InboxRepository = {
    enqueue: vi.fn().mockResolvedValue("inserted"),
    claim: vi.fn().mockResolvedValue([]),
    storeReply: vi.fn().mockResolvedValue(undefined),
    markSent: vi.fn().mockResolvedValue(undefined),
    markFailure: vi.fn().mockResolvedValue(undefined),
  };
  return {
    repo,
    telegram: { sendMessage: vi.fn().mockResolvedValue(undefined) },
    webhookSecret: "hook-secret",
    inboxBatchSize: 10,
    ...overrides,
  };
}

describe("Telegram webhook", () => {
  it("rejects a wrong webhook secret", async () => {
    const runtime = deps();
    const response = await handleWebhook(request(validUpdate, "wrong"), runtime);
    expect(response.status).toBe(401);
    expect(runtime.repo.enqueue).not.toHaveBeenCalled();
  });

  it("rejects malformed and non-private updates", async () => {
    const runtime = deps();
    expect((await handleWebhook(request({ update_id: "88" }), runtime)).status).toBe(400);
    const groupUpdate = structuredClone(validUpdate);
    groupUpdate.message.chat.type = "group";
    expect((await handleWebhook(request(groupUpdate), runtime)).status).toBe(200);
    expect(runtime.repo.enqueue).not.toHaveBeenCalled();
  });

  it("persists an accepted message and reports duplicates idempotently", async () => {
    const runtime = deps();
    const first = await handleWebhook(request(), runtime);
    expect(first.status).toBe(200);
    expect(await first.json()).toEqual({ ok: true, duplicate: false });
    expect(runtime.repo.enqueue).toHaveBeenCalledWith(validUpdate);

    vi.mocked(runtime.repo.enqueue).mockResolvedValueOnce("duplicate");
    const duplicate = await handleWebhook(request(), runtime);
    expect(await duplicate.json()).toEqual({ ok: true, duplicate: true });
  });

  it("asks Telegram to retry when durable inbox storage is unavailable", async () => {
    const runtime = deps();
    vi.mocked(runtime.repo.enqueue).mockRejectedValueOnce(new Error("database unavailable"));
    expect((await handleWebhook(request(), runtime)).status).toBe(503);
  });
});

describe("inbox delivery", () => {
  it("sends a reply and marks the update processed", async () => {
    const runtime = deps({
      repo: {
        enqueue: vi.fn(),
        claim: vi.fn().mockResolvedValue([
          { update_id: 88, telegram_user_id: 123, chat_id: 123, message_text: "hi", attempts: 1, lease_token: "lease-1" },
        ]),
        storeReply: vi.fn().mockResolvedValue(undefined),
    markSent: vi.fn().mockResolvedValue(undefined),
        markFailure: vi.fn(),
      },
    });
    expect(await processInboxBatch(runtime)).toEqual({ processed: 1, failed: 0 });
    expect(runtime.telegram.sendMessage).toHaveBeenCalledWith(
      123,
      "메시지를 받았어요.",
    );
    expect(runtime.repo.markSent).toHaveBeenCalledWith(88, "lease-1");
  });

  it("records failed Telegram delivery for retry", async () => {
    const runtime = deps({
      repo: {
        enqueue: vi.fn(),
        claim: vi.fn().mockResolvedValue([
          { update_id: 88, telegram_user_id: 123, chat_id: 123, message_text: "hi", attempts: 1, lease_token: "lease-1" },
        ]),
        storeReply: vi.fn().mockResolvedValue(undefined),
    markSent: vi.fn(),
        markFailure: vi.fn().mockResolvedValue(undefined),
      },
      telegram: { sendMessage: vi.fn().mockRejectedValue(new Error("temporary failure")) },
    });
    expect(await processInboxBatch(runtime)).toEqual({ processed: 0, failed: 1 });
    expect(runtime.repo.markFailure).toHaveBeenCalledWith(88, "lease-1", "TELEGRAM_SEND_FAILED", 1);
  });
});


it.each(["LLM_NETWORK_FAILED", "LLM_TIMEOUT"] as const)('bounds retries and saves a failure reply for %s', async code => {
  const runtime = deps({ assistant: { respond: vi.fn().mockRejectedValue(new LlmRequestError(code)) } });
  const message = { update_id: 88, telegram_user_id: 123, chat_id: 123, message_text: "hi", attempts: 1, lease_token: "lease-1" };
  vi.mocked(runtime.repo.claim).mockResolvedValue([message]);
  expect(await processInboxBatch(runtime)).toEqual({ processed: 0, failed: 1 });
  expect(runtime.repo.markFailure).toHaveBeenCalledWith(88, "lease-1", code, 1);
  expect(runtime.telegram.sendMessage).not.toHaveBeenCalled();
  message.attempts = 2;
  expect(await processInboxBatch(runtime)).toEqual({ processed: 1, failed: 0 });
  const reply = vi.mocked(runtime.repo.storeReply).mock.calls[0][2];
  expect(reply).toContain("일부 변경이 저장됐을 수");
  vi.mocked(runtime.repo.claim).mockResolvedValue([{ ...message, response_text: reply, attempts: 3 }]);
  await processInboxBatch(runtime);
  expect(runtime.assistant!.respond).toHaveBeenCalledTimes(2);
});
