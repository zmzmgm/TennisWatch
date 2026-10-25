import type { TokenUsage } from "../types";

export interface ToolDefinition {
  type: "function";
  function: {
    name: string;
    description: string;
    parameters: Record<string, unknown>;
    strict: false;
  };
}

export interface ToolContext {
  /** Set from the verified Telegram update; never supplied by the model. */
  telegramUserId: number;
  chatId: number;
  updateId: number;
  toolCallId: string;
}

export interface ToolResult {
  ok: boolean;
  code?: string;
  message?: string;
  data?: unknown;
}

export interface RegisteredTool {
  definition: ToolDefinition;
  validate(input: unknown): Record<string, unknown> | null;
  execute(context: ToolContext, input: Record<string, unknown>): Promise<ToolResult>;
}

const objectSchema = (
  properties: Record<string, unknown>,
  required: string[] = [],
): Record<string, unknown> => ({
  type: "object",
  properties,
  required,
  additionalProperties: false,
});

const text = { type: "string", minLength: 1, maxLength: 120 };
const date = { type: "string", description: "YYYY-MM-DD in Asia/Seoul" };
const clock = { type: "string", description: "24-hour local time HH:MM" };
const integerId = { type: "integer", minimum: 1 };
const audience = { type: "string", enum: ["resident", "general", "all"] };
const weekday = { type: "integer", minimum: 1, maximum: 7 };

export const toolDefinitions: ToolDefinition[] = [
  { type: "function", function: { name: "search_facilities", description: "Search supported tennis facilities by district or name.", parameters: objectSchema({ region: text, query: text }), strict: false } },
  { type: "function", function: { name: "search_availability", description: "Search the stored availability observations for a local date and time range. Include observation freshness.", parameters: objectSchema({ region: text, facility: text, date, after_time: clock, before_time: clock }, ["date"]), strict: false } },
  { type: "function", function: { name: "get_opening_schedule", description: "Look up resident and general opening times from verified reservation policies.", parameters: objectSchema({ facility: text, date }, ["facility", "date"]), strict: false } },
  { type: "function", function: { name: "create_subscription", description: "Create an availability or opening reminder watch for the authenticated Telegram user.", parameters: objectSchema({ facility: text, region: text, play_date: date, weekdays: { type: "array", items: weekday, minItems: 1, maxItems: 7 }, start_time: clock, end_time: clock, watch_type: { type: "string", enum: ["availability", "opening"] }, audience }, ["watch_type"]), strict: false } },
  { type: "function", function: { name: "update_subscription", description: "Change an existing watch belonging to the authenticated Telegram user.", parameters: objectSchema({ subscription_id: integerId, patch: objectSchema({ facility: text, region: text, play_date: date, weekdays: { type: "array", items: weekday, minItems: 1, maxItems: 7 }, start_time: clock, end_time: clock, watch_type: { type: "string", enum: ["availability", "opening"] }, audience, enabled: { type: "boolean"} }) }, ["subscription_id", "patch"]), strict: false } },
  { type: "function", function: { name: "delete_subscription", description: "Disable a watch belonging to the authenticated Telegram user.", parameters: objectSchema({ subscription_id: integerId }, ["subscription_id"]), strict: false } },
  { type: "function", function: { name: "list_subscriptions", description: "List active watches belonging to the authenticated Telegram user.", parameters: objectSchema({}), strict: false } },
];

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const validText = (value: unknown): value is string =>
  typeof value === "string" && value.trim().length > 0 && value.trim().length <= 120;

const validDate = (value: unknown): value is string => {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00.000Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
};

const validTime = (value: unknown): value is string =>
  typeof value === "string" && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);

function onlyKeys(input: Record<string, unknown>, allowed: string[]): boolean {
  return Object.keys(input).every((key) => allowed.includes(key));
}

function basicValidate(name: string, value: unknown): Record<string, unknown> | null {
  if (!isPlainObject(value)) return null;
  switch (name) {
    case "search_facilities":
      return onlyKeys(value, ["region", "query"]) &&
        (value.region === undefined || validText(value.region)) &&
        (value.query === undefined || validText(value.query)) &&
        (value.region !== undefined || value.query !== undefined) ? value : null;
    case "search_availability":
      return onlyKeys(value, ["region", "facility", "date", "after_time", "before_time"]) &&
        validDate(value.date) &&
        (value.region === undefined || validText(value.region)) &&
        (value.facility === undefined || validText(value.facility)) &&
        (value.after_time === undefined || validTime(value.after_time)) &&
        (value.before_time === undefined || validTime(value.before_time)) ? value : null;
    case "get_opening_schedule":
      return onlyKeys(value, ["facility", "date"]) && validText(value.facility) && validDate(value.date) ? value : null;
    case "create_subscription": {
      const fields = ["facility", "region", "play_date", "weekdays", "start_time", "end_time", "watch_type", "audience"];
      if (!onlyKeys(value, fields) || !["availability", "opening"].includes(String(value.watch_type))) return null;
      if (value.audience !== undefined && !["resident", "general", "all"].includes(String(value.audience))) return null;
      if (value.facility !== undefined && !validText(value.facility)) return null;
      if (value.region !== undefined && !validText(value.region)) return null;
      if (value.play_date !== undefined && !validDate(value.play_date)) return null;
      if (value.weekdays !== undefined && (!Array.isArray(value.weekdays) || value.weekdays.length < 1 || value.weekdays.length > 7 || !value.weekdays.every((day) => Number.isInteger(day) && Number(day) >= 1 && Number(day) <= 7))) return null;
      if (value.start_time !== undefined && !validTime(value.start_time)) return null;
      if (value.end_time !== undefined && !validTime(value.end_time)) return null;
      if (value.play_date !== undefined && value.weekdays !== undefined) return null;
      if (value.start_time !== undefined && value.end_time !== undefined && String(value.end_time) <= String(value.start_time)) return null;
      return value.facility !== undefined || value.region !== undefined ? value : null;
    }
    case "update_subscription": {
      if (!onlyKeys(value, ["subscription_id", "patch"]) || !Number.isSafeInteger(value.subscription_id) || Number(value.subscription_id) < 1 || !isPlainObject(value.patch)) return null;
      const patch = value.patch;
      if (!onlyKeys(patch, ["facility", "region", "play_date", "weekdays", "start_time", "end_time", "watch_type", "enabled", "audience"]) || Object.keys(patch).length === 0) return null;
      if (patch.facility !== undefined && !validText(patch.facility)) return null;
      if (patch.region !== undefined && !validText(patch.region)) return null;
      if (patch.play_date !== undefined && !validDate(patch.play_date)) return null;
      if (patch.weekdays !== undefined && (!Array.isArray(patch.weekdays) || patch.weekdays.length < 1 || patch.weekdays.length > 7 || !patch.weekdays.every((day) => Number.isInteger(day) && Number(day) >= 1 && Number(day) <= 7))) return null;
      if (patch.start_time !== undefined && !validTime(patch.start_time)) return null;
      if (patch.end_time !== undefined && !validTime(patch.end_time)) return null;
      if (patch.watch_type !== undefined && !["availability", "opening"].includes(String(patch.watch_type))) return null;
      if (patch.audience !== undefined && !["resident", "general", "all"].includes(String(patch.audience))) return null;
      if (patch.enabled !== undefined && typeof patch.enabled !== "boolean") return null;
      if (patch.play_date !== undefined && patch.weekdays !== undefined) return null;
      return value;
    }
    case "delete_subscription":
      return onlyKeys(value, ["subscription_id"]) && Number.isSafeInteger(value.subscription_id) && Number(value.subscription_id) > 0 ? value : null;
    case "list_subscriptions":
      return Object.keys(value).length === 0 ? value : null;
    default:
      return null;
  }
}

export type ToolHandler = (context: ToolContext, input: Record<string, unknown>) => Promise<ToolResult>;

export class ToolRegistry {
  private readonly byName = new Map<string, RegisteredTool>();

  constructor(handlers: Partial<Record<string, ToolHandler>> = {}) {
    for (const definition of toolDefinitions) {
      const name = definition.function.name;
      this.byName.set(name, {
        definition,
        validate: (input) => basicValidate(name, input),
        execute: handlers[name] ?? (async () => ({
          ok: false,
          code: "not_ready",
          message: "이 기능은 아직 데이터베이스 연결이 완료되지 않았습니다. 완료했다고 안내하지 마세요.",
        })),
      });
    }
  }

  definitions(): ToolDefinition[] {
    return toolDefinitions;
  }

  async execute(name: string, rawInput: unknown, context: ToolContext): Promise<ToolResult> {
    const tool = this.byName.get(name);
    if (!tool) return { ok: false, code: "unknown_tool", message: "허용되지 않은 도구입니다." };
    const input = tool.validate(rawInput);
    if (!input) return { ok: false, code: "invalid_input", message: "도구 입력값이 올바르지 않습니다." };
    return tool.execute(context, input);
  }
}

export function usageFrom(value: unknown): TokenUsage | undefined {
  if (!isPlainObject(value)) return undefined;
  const prompt = Number(value.prompt_tokens);
  const completion = Number(value.completion_tokens);
  if (!Number.isFinite(prompt) && !Number.isFinite(completion)) return undefined;
  return {
    ...(Number.isFinite(prompt) ? { inputTokens: prompt } : {}),
    ...(Number.isFinite(completion) ? { outputTokens: completion } : {}),
  };
}
