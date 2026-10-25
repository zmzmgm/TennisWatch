import type { Env } from "./types";

export function allowedChats(env: Env): ReadonlySet<number> {
  const values = (env.ALLOWED_TELEGRAM_CHAT_IDS ?? "").split(",").map((value) => value.trim());
  if (!values.length || values.some((value) => !/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) <= 0)) {
    throw new Error("WORKER_NOT_CONFIGURED");
  }
  return new Set(values.map(Number));
}

export function validateEnv(env: Env): void {
  const required = [env.TELEGRAM_BOT_TOKEN, env.TELEGRAM_WEBHOOK_SECRET, env.SUPABASE_URL,
    env.SUPABASE_SERVICE_ROLE_KEY, env.LLM_API_KEY, env.LLM_MODEL];
  if (required.some((value) => !value?.trim()) || !/^[A-Za-z0-9_-]{16,256}$/.test(env.TELEGRAM_WEBHOOK_SECRET)) {
    throw new Error("WORKER_NOT_CONFIGURED");
  }
  for (const value of [env.SUPABASE_URL, env.LLM_API_URL ?? "https://api.openai.com/v1/chat/completions"]) {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.search) throw new Error("WORKER_NOT_CONFIGURED");
  }
  for (const [raw, minimum, maximum] of [
    [env.LLM_DAILY_USER_CALLS, 1, 100], [env.LLM_DAILY_GLOBAL_CALLS, 1, 1000],
    [env.LLM_DAILY_TOKEN_BUDGET, 1, 1000000], [env.LLM_MAX_TOKENS, 100, 1200],
  ] as const) {
    if (raw !== undefined && (!Number.isInteger(Number(raw)) || Number(raw) < minimum || Number(raw) > maximum)) {
      throw new Error("WORKER_NOT_CONFIGURED");
    }
  }
  allowedChats(env);
}
