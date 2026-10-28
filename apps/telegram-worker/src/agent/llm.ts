import type { TokenUsage } from "../types";
import type { ToolDefinition } from "./tools";

export interface LlmToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
  extra_content?: { google: { thought_signature: string } };
}

export type LlmMessage =
  | { role: "system" | "user"; content: string }
  | { role: "assistant"; content: string | null; tool_calls?: LlmToolCall[] }
  | { role: "tool"; tool_call_id: string; content: string };

export interface LlmCompletion {
  message: Extract<LlmMessage, { role: "assistant" }>;
  usage?: TokenUsage;
}

export interface LlmClient {
  complete(messages: LlmMessage[], tools: ToolDefinition[]): Promise<LlmCompletion>;
}

interface ChatCompletionsResponse {
  choices?: Array<{
    message?: {
      content?: unknown;
      tool_calls?: Array<{
        id?: unknown;
        extra_content?: { google?: { thought_signature?: unknown } };
        function?: { name?: unknown; arguments?: unknown };
      }>;
    };
  }>;
  usage?: { prompt_tokens?: unknown; completion_tokens?: unknown };
}

export const LLM_FAILURE_CODES = [
  "LLM_AUTH_FAILED", "LLM_ACCESS_DENIED", "LLM_MODEL_NOT_FOUND", "LLM_QUOTA_EXCEEDED",
  "LLM_RATE_LIMITED", "LLM_INVALID_REQUEST", "LLM_PROVIDER_ERROR", "LLM_NETWORK_FAILED", "LLM_TIMEOUT",
] as const;
const LLM_REQUEST_TIMEOUT_MS = 30_000;
export type LlmFailureCode = typeof LLM_FAILURE_CODES[number];
export class LlmRequestError extends Error {
  constructor(readonly code: LlmFailureCode) { super(code); }
}

export class OpenAICompatibleClient implements LlmClient {
  constructor(
    private readonly options: {
      apiKey: string;
      model: string;
      endpoint?: string;
      maxTokens?: number;
      fetcher?: typeof fetch;
    },
  ) {}

  async complete(messages: LlmMessage[], tools: ToolDefinition[]): Promise<LlmCompletion> {
    if (!this.options.apiKey || !this.options.model) throw new Error("LLM configuration is missing");
    const fetcher = this.options.fetcher ?? fetch;
    let response: Response;
    const signal = AbortSignal.timeout(LLM_REQUEST_TIMEOUT_MS);
    try {
      response = await fetcher(this.options.endpoint ?? "https://api.openai.com/v1/chat/completions", {
      method: "POST",
      signal,
      headers: {
        Authorization: `Bearer ${this.options.apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: this.options.model,
        messages,
        tools,
        tool_choice: "auto",
        parallel_tool_calls: false,
        max_tokens: this.options.maxTokens ?? 700,
      }),
    });
    } catch (error) {
      throw new LlmRequestError(signal.aborted || (error instanceof Error && error.name === "TimeoutError")
        ? "LLM_TIMEOUT" : "LLM_NETWORK_FAILED");
    }
    if (!response.ok) {
      let code: LlmFailureCode;
      switch (response.status) {
        case 401: code = "LLM_AUTH_FAILED"; break;
        case 403: code = "LLM_ACCESS_DENIED"; break;
        case 404: code = "LLM_MODEL_NOT_FOUND"; break;
        case 429: {
          code = "LLM_RATE_LIMITED";
          try {
            const body = await response.json() as { error?: { code?: unknown; type?: unknown } };
            if (body?.error?.code === "insufficient_quota" || body?.error?.type === "insufficient_quota") {
              code = "LLM_QUOTA_EXCEEDED";
            }
          } catch { /* Response text may contain secrets. Never retain it. */ }
          break;
        }
        case 400: case 422: code = "LLM_INVALID_REQUEST"; break;
        default: code = "LLM_PROVIDER_ERROR";
      }
      throw new LlmRequestError(code);
    }

    let payload: ChatCompletionsResponse;
    try {
      payload = (await response.json()) as ChatCompletionsResponse;
    } catch (error) {
      if (signal.aborted || (error instanceof Error && error.name === "TimeoutError")) throw new LlmRequestError("LLM_TIMEOUT");
      if (error instanceof TypeError) throw new LlmRequestError("LLM_NETWORK_FAILED");
      throw new Error("LLM response was not valid JSON");
    }
    const rawMessage = payload.choices?.[0]?.message;
    if (!rawMessage) throw new Error("LLM response did not include a message");

    const toolCalls: LlmToolCall[] = [];
    for (const call of rawMessage.tool_calls ?? []) {
      const id = call.id;
      const name = call.function?.name;
      const args = call.function?.arguments;
      if (typeof id !== "string" || typeof name !== "string" || typeof args !== "string") {
        throw new Error("LLM returned an invalid tool call");
      }
      const signature = call.extra_content?.google?.thought_signature;
      if (signature !== undefined && (typeof signature !== "string" || !signature.length || signature.length > 65536)) {
        throw new Error("LLM returned invalid tool metadata");
      }
      toolCalls.push({
        id: id.slice(0, 200),
        type: "function",
        function: { name: name.slice(0, 100), arguments: args.slice(0, 8000) },
        // Gemini requires this opaque value unchanged on the next tool-result turn.
        // Protocol metadata is never a tool argument, log, or DB field.
        ...(typeof signature === "string" ? { extra_content: { google: { thought_signature: signature } } } : {}),
      });
    }
    const content = typeof rawMessage.content === "string" ? rawMessage.content.slice(0, 6000) : null;
    return {
      message: { role: "assistant", content, ...(toolCalls.length ? { tool_calls: toolCalls } : {}) },
      ...(payload.usage ? { usage: {
        ...(typeof payload.usage.prompt_tokens === "number" ? { inputTokens: payload.usage.prompt_tokens } : {}),
        ...(typeof payload.usage.completion_tokens === "number" ? { outputTokens: payload.usage.completion_tokens } : {}),
      } } : {}),
    };
  }
}
