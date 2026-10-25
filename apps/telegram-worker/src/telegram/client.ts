import type { TelegramSender } from "../types";

export class TelegramBotClient implements TelegramSender {
  constructor(private readonly token: string) {}

  async sendMessage(chatId: number, text: string): Promise<void> {
    const response = await fetch(`https://api.telegram.org/bot${this.token}/sendMessage`, {
      method: "POST",
      signal: AbortSignal.timeout(8000),
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
    });
    if (!response.ok) throw new Error(`Telegram sendMessage failed (${response.status})`);
    const result = (await response.json()) as { ok?: boolean };
    if (!result.ok) throw new Error("Telegram sendMessage returned an unsuccessful result");
  }
}
