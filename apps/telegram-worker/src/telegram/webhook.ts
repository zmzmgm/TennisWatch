import { readTelegramUpdate, validSecret } from "./schemas";
import type { RuntimeDeps } from "../types";
import { LlmRequestError } from "../agent/llm";
import { BudgetExceededError } from "../agent/budget";

function json(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

export async function handleWebhook(request: Request, deps: RuntimeDeps): Promise<Response> {
  if (request.method !== "POST") return json({ ok: false, error: "method_not_allowed" }, 405);
  if (!validSecret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), deps.webhookSecret)) {
    return json({ ok: false, error: "unauthorized" }, 401);
  }

  const update = await readTelegramUpdate(request);
  if (!update) return json({ ok: false, error: "invalid_update" }, 400);
  if (update === "ignored") return json({ ok: true, ignored: true });
  if (deps.allowedChatIds && !deps.allowedChatIds.has(update.message.chat.id)) {
    return json({ ok: true, ignored: true });
  }

  try {
    const result = await deps.repo.enqueue(update);
    return json({ ok: true, duplicate: result === "duplicate" });
  } catch {
    // Telegram retries non-2xx deliveries. Never log tokens, body text, or DB response bodies.
    return json({ ok: false, error: "temporarily_unavailable" }, 503);
  }
}

const RECEIVED_MESSAGE = "메시지를 받았어요.";

export async function processInboxBatch(deps: RuntimeDeps): Promise<{ processed: number; failed: number }> {
  // Bound each invocation's subrequest count and CPU work for the Free plan.
  const messages = await deps.repo.claim(1);
  let processed = 0;
  let failed = 0;
  for (const message of messages) {
    let failureCode = "PROCESSING_FAILED";
    try {
      if (deps.allowedChatIds && !deps.allowedChatIds.has(message.chat_id)) {
        await deps.repo.markSent(message.update_id, message.lease_token);
        processed += 1;
        continue;
      }
      let text = message.response_text;
      if (!text) {
        failureCode = "CONTEXT_LOAD_FAILED";
        const previousContext = deps.conversations
          ? await deps.conversations.load(message.telegram_user_id, message.chat_id)
          : null;
        failureCode = "AGENT_FAILED";
        let reply;
        try {
          reply = deps.assistant
            ? await deps.assistant.respond(message.telegram_user_id, message.message_text, message.update_id, previousContext, message.chat_id)
            : { text: RECEIVED_MESSAGE };
        } catch (error) {
          if (error instanceof BudgetExceededError) {
            reply = { text: "오늘의 AI 요청 한도에 도달했어요. 다음 날 다시 요청해 주세요." };
          } else if (error instanceof LlmRequestError && ["LLM_AUTH_FAILED", "LLM_ACCESS_DENIED",
            "LLM_MODEL_NOT_FOUND", "LLM_QUOTA_EXCEEDED", "LLM_INVALID_REQUEST"].includes(error.code)) {
            reply = { text: error.code === "LLM_QUOTA_EXCEEDED"
              ? "AI 서비스의 이용 한도가 부족해 요청을 처리하지 못했어요. API 크레딧과 결제·사용 한도 설정을 확인해 주세요."
              : "AI 연결 설정 오류로 요청을 처리하지 못했어요. API 키와 모델 접근 권한을 확인해 주세요." };
          } else if (error instanceof LlmRequestError &&
            ["LLM_NETWORK_FAILED", "LLM_TIMEOUT"].includes(error.code) && message.attempts >= 2) {
            reply = { text: "AI 연결이 반복해서 실패해 자동 재시도를 중단했어요. 요청의 최종 결과를 확인하지 못했어요. 일부 변경이 저장됐을 수 있으니 대시보드에서 감시 조건을 확인해 주세요." };
          } else {
            throw error;
          }
        }
        if (deps.conversations && reply.context) {
          failureCode = "CONTEXT_SAVE_FAILED";
          await deps.conversations.save(message.telegram_user_id, message.chat_id, reply.context);
        }
        if (reply.usage) {
          console.log(JSON.stringify({ event: "llm_usage", ...reply.usage }));
        }
        text = reply.text;
        failureCode = "REPLY_STORE_FAILED";
        await deps.repo.storeReply(message.update_id, message.lease_token, text);
      }
      failureCode = "TELEGRAM_SEND_FAILED";
      await deps.telegram.sendMessage(message.chat_id, text);
      failureCode = "INBOX_COMPLETE_FAILED";
      await deps.repo.markSent(message.update_id, message.lease_token);
      processed += 1;
    } catch (error) {
      if (error instanceof LlmRequestError) failureCode = error.code;
      try {
        await deps.repo.markFailure(message.update_id, message.lease_token, failureCode, message.attempts);
      } catch {
        // Expired processing leases are reclaimed by the database function.
      }
      failed += 1;
    }
  }
  return { processed, failed };
}
