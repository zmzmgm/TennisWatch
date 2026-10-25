import type { Env, InboxMessage, InboxRepository, TelegramTextUpdate } from "../types";

export class SupabaseInboxRepository implements InboxRepository {
  private readonly baseUrl: string;

  constructor(private readonly env: Pick<Env, "SUPABASE_URL" | "SUPABASE_SERVICE_ROLE_KEY">) {
    this.baseUrl = `${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1`;
  }

  private headers(extra: Record<string, string> = {}): HeadersInit {
    return {
      apikey: this.env.SUPABASE_SERVICE_ROLE_KEY,
      Authorization: `Bearer ${this.env.SUPABASE_SERVICE_ROLE_KEY}`,
      "Content-Type": "application/json",
      ...extra,
    };
  }

  private async requireOk(response: Response, operation: string): Promise<void> {
    if (!response.ok) {
      // Do not expose response bodies: PostgREST errors may contain user data.
      throw new Error(`Supabase ${operation} failed (${response.status})`);
    }
  }

  async enqueue(update: TelegramTextUpdate): Promise<"inserted" | "duplicate"> {
    const response = await fetch(`${this.baseUrl}/telegram_updates`, {
      method: "POST",
      signal: AbortSignal.timeout(8000),
      headers: this.headers({ Prefer: "resolution=ignore-duplicates,return=representation" }),
      body: JSON.stringify({
        update_id: update.update_id,
        telegram_user_id: update.message.from.id,
        chat_id: update.message.chat.id,
        message_text: update.message.text,
        status: "pending",
      }),
    });
    await this.requireOk(response, "enqueue");
    const inserted = (await response.json()) as unknown[];
    return inserted.length > 0 ? "inserted" : "duplicate";
  }

  async claim(limit: number): Promise<InboxMessage[]> {
    const response = await fetch(`${this.baseUrl}/rpc/claim_telegram_updates`, {
      method: "POST",
      signal: AbortSignal.timeout(8000),
      headers: this.headers(),
      body: JSON.stringify({ p_limit: limit }),
    });
    await this.requireOk(response, "claim");
    return (await response.json()) as InboxMessage[];
  }

  private async updateOwned(updateId: number, leaseToken: string, body: Record<string, unknown>): Promise<void> {
    const url = new URL(`${this.baseUrl}/telegram_updates`);
    url.searchParams.set("update_id", `eq.${updateId}`);
    url.searchParams.set("status", "eq.processing");
    url.searchParams.set("lease_token", `eq.${leaseToken}`);
    const response = await fetch(url.toString(), {
      method: "PATCH", signal: AbortSignal.timeout(8000),
      headers: this.headers({ Prefer: "return=representation" }), body: JSON.stringify(body),
    });
    await this.requireOk(response, "update inbox");
    const changed = await response.json() as unknown[];
    if (changed.length !== 1) throw new Error("INBOX_LEASE_LOST");
  }

  async storeReply(updateId: number, leaseToken: string, text: string): Promise<void> {
    await this.updateOwned(updateId, leaseToken, { response_text: text.slice(0, 3500) });
  }

  async markSent(updateId: number, leaseToken: string): Promise<void> {
    await this.updateOwned(updateId, leaseToken, {
      status: "processed", processed_at: new Date().toISOString(), lease_until: null, lease_token: null,
    });
  }

  async markFailure(updateId: number, leaseToken: string, message: string, attempts: number): Promise<void> {
    await this.updateOwned(updateId, leaseToken, {
      status: attempts >= 5 ? "failed" : "pending",
      next_attempt_at: new Date(Date.now() + Math.min(3_600_000, 60_000 * 2 ** Math.max(0, attempts - 1))).toISOString(),
      lease_until: null, lease_token: null, last_error: message.slice(0, 100),
    });
  }
}
