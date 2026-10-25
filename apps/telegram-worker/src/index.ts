import { SupabaseInboxRepository } from "./db/supabase-inbox";
import { TelegramBotClient } from "./telegram/client";
import { handleWebhook, processInboxBatch } from "./telegram/webhook";
import { OpenAICompatibleClient } from "./agent/llm";
import { AgentRunner } from "./agent/runner";
import { ToolRegistry } from "./agent/tools";
import { SupabaseTennisTools } from "./db/tennis-tools";
import { BudgetedLlmClient, SupabaseBudgetStore } from "./agent/budget";
import { allowedChats, validateEnv } from "./config";
import { validSecret } from "./telegram/schemas";
import type { ConversationState, Env, ExecutionContextLike, ScheduledController } from "./types";

function dependencies(env: Env) {
  validateEnv(env);
  const tennis = new SupabaseTennisTools(env);
  const maxTokens = Math.min(1200, Math.max(100, Number.parseInt(env.LLM_MAX_TOKENS ?? "700", 10) || 700));
  return {
    repo: new SupabaseInboxRepository(env),
    telegram: new TelegramBotClient(env.TELEGRAM_BOT_TOKEN),
    assistant: { respond(userId: number, message: string, updateId: number, context?: ConversationState | null, chatId?: number) {
      const model = new OpenAICompatibleClient({
        apiKey: env.LLM_API_KEY ?? "",
        model: env.LLM_MODEL ?? "",
        endpoint: env.LLM_API_URL,
        maxTokens,
      });
      return new AgentRunner(new BudgetedLlmClient(model, new SupabaseBudgetStore(env, userId, updateId), maxTokens),
        new ToolRegistry(tennis.handlers())).respond(userId, message, updateId, context, chatId);
    } },
    conversations: tennis,
    webhookSecret: env.TELEGRAM_WEBHOOK_SECRET,
    inboxBatchSize: 1,
    allowedChatIds: allowedChats(env),
  };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET") {
      return new Response("ok", { status: 200, headers: { "Content-Type": "text/plain" } });
    }
    if (url.pathname === "/ready" && request.method === "GET") {
      if (!validSecret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), env.TELEGRAM_WEBHOOK_SECRET)) {
        return new Response("unauthorized", { status: 401 });
      }
      try {
        validateEnv(env);
        const response = await fetch(`${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/rpc/worker_readiness`, {
          method: "POST", signal: AbortSignal.timeout(8000),
          headers: { apikey: env.SUPABASE_SERVICE_ROLE_KEY, Authorization: `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`,
            "Content-Type": "application/json" }, body: "{}",
        });
        if (response.ok && await response.json() === true) return new Response("ready");
      } catch { /* Do not expose configuration or DB responses. */ }
      return new Response("not ready", { status: 503 });
    }
    if (url.pathname !== "/telegram/webhook") {
      return new Response("not found", { status: 404 });
    }
    try { return await handleWebhook(request, dependencies(env)); }
    catch { return new Response("temporarily unavailable", { status: 503 }); }
  },

  async scheduled(_controller: ScheduledController, env: Env, _ctx: ExecutionContextLike): Promise<void> {
    await processInboxBatch(dependencies(env));
  },
};
