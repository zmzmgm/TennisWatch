import { LlmRequestError, type LlmFailureCode } from "./llm";
import type { Env, TokenUsage } from "../types";
import type { LlmClient, LlmCompletion, LlmMessage } from "./llm";
import type { ToolDefinition } from "./tools";

export class BudgetExceededError extends Error {
  constructor() { super("LLM_BUDGET_EXCEEDED"); }
}

export interface BudgetStore {
  reserve(requestKey: string, tokens: number): Promise<number>;
  finish(id: number, requestKey: string, usage?: TokenUsage, failed?: boolean, failureCode?: LlmFailureCode): Promise<void>;
}

export class SupabaseBudgetStore implements BudgetStore {
  constructor(private readonly env: Env, private readonly userId: number, private readonly updateId: number) {}

  private headers(): HeadersInit {
    return { apikey: this.env.SUPABASE_SERVICE_ROLE_KEY, Authorization: `Bearer ${this.env.SUPABASE_SERVICE_ROLE_KEY}`,
      "Content-Type": "application/json", Prefer: "return=representation" };
  }

  async reserve(requestKey: string, tokens: number): Promise<number> {
    const response = await fetch(`${this.env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/rpc/reserve_llm_call`, {
      method: "POST", headers: this.headers(), signal: AbortSignal.timeout(8000),
      body: JSON.stringify({ p_user_id: this.userId, p_update_id: this.updateId, p_request_key: requestKey,
        p_model: this.env.LLM_MODEL, p_reserved_tokens: tokens,
        p_user_limit: Number(this.env.LLM_DAILY_USER_CALLS ?? 20),
        p_global_limit: Number(this.env.LLM_DAILY_GLOBAL_CALLS ?? 100),
        p_token_budget: Number(this.env.LLM_DAILY_TOKEN_BUDGET ?? 100000) }),
    });
    if (!response.ok) throw new Error("LLM_BUDGET_STORE_UNAVAILABLE");
    const result = await response.json() as { ok: boolean; code?: string; reservation_id?: number };
    if (result.code === "LLM_BUDGET_EXCEEDED") throw new BudgetExceededError();
    if (!result.ok || !Number.isSafeInteger(result.reservation_id)) throw new Error("LLM_BUDGET_RESERVATION_FAILED");
    return result.reservation_id!;
  }

  async finish(id: number, requestKey: string, usage?: TokenUsage, failed = false, failureCode?: LlmFailureCode): Promise<void> {
    const count = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
    const url = new URL(`${this.env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/llm_usage`);
    url.searchParams.set("id", `eq.${id}`);
    url.searchParams.set("request_key", `eq.${requestKey}`);
    url.searchParams.set("status", "eq.RESERVED");
    const response = await fetch(url.toString(), { method: "PATCH", headers: this.headers(), signal: AbortSignal.timeout(8000),
      body: JSON.stringify({ status: failed ? "FAILED" : "COMPLETE", input_tokens: count(usage?.inputTokens),
        output_tokens: count(usage?.outputTokens), error_code: failed ? (failureCode ?? "LLM_REQUEST_FAILED") : null }),
    });
    if (!response.ok) throw new Error("LLM_BUDGET_RECORD_FAILED");
  }
}

export class BudgetedLlmClient implements LlmClient {
  constructor(private readonly inner: LlmClient, private readonly store: BudgetStore, private readonly maxTokens = 700) {}

  async complete(messages: LlmMessage[], tools: ToolDefinition[]): Promise<LlmCompletion> {
    const key = crypto.randomUUID();
    // Reserve conservatively by UTF-8 bytes. Exact billed tokens depend on the provider.
    const reserved = new TextEncoder().encode(JSON.stringify({ messages, tools })).length + this.maxTokens;
    if (reserved > 100000) throw new BudgetExceededError();
    const id = await this.store.reserve(key, reserved);
    let result: LlmCompletion;
    try {
      result = await this.inner.complete(messages, tools);
    } catch (error) {
      try {
        if (error instanceof LlmRequestError) await this.store.finish(id, key, undefined, true, error.code);
        else await this.store.finish(id, key, undefined, true);
      } catch { /* The reservation still counts. */ }
      if (error instanceof LlmRequestError) throw error;
      throw new Error("LLM_REQUEST_FAILED");
    }
    await this.store.finish(id, key, result.usage);
    return result;
  }
}
