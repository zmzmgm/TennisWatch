import type { TelegramTextUpdate } from "../types";

const MAX_BODY_BYTES = 16 * 1024;
const MAX_MESSAGE_LENGTH = 4096;

function isSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value);
}

export function parseTextUpdate(value: unknown): TelegramTextUpdate | "ignored" | null {
  if (!value || typeof value !== "object") return null;
  const update = value as Record<string, unknown>;
  if (!isSafeInteger(update.update_id)) return null;
  if (!update.message || typeof update.message !== "object") return "ignored";

  const message = update.message as Record<string, unknown>;
  const from = message.from as Record<string, unknown> | undefined;
  const chat = message.chat as Record<string, unknown> | undefined;
  if (!from || !chat || typeof message.text !== "string") return "ignored";
  if (!isSafeInteger(from.id) || from.is_bot === true) return "ignored";
  if (!isSafeInteger(chat.id) || chat.type !== "private") return "ignored";
  if (message.text.length === 0 || message.text.length > MAX_MESSAGE_LENGTH) return "ignored";

  return value as TelegramTextUpdate;
}

export async function readTelegramUpdate(request: Request): Promise<TelegramTextUpdate | "ignored" | null> {
  const lengthHeader = request.headers.get("content-length");
  if (lengthHeader && Number(lengthHeader) > MAX_BODY_BYTES) return null;
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) return null;
  try {
    return parseTextUpdate(JSON.parse(body) as unknown);
  } catch {
    return null;
  }
}

export function validSecret(received: string | null, expected: string): boolean {
  if (!received || !expected) return false;
  const encoder = new TextEncoder();
  const left = encoder.encode(received);
  const right = encoder.encode(expected);
  if (left.length !== right.length) return false;
  let mismatch = 0;
  for (let i = 0; i < left.length; i += 1) mismatch |= left[i] ^ right[i];
  return mismatch === 0;
}
