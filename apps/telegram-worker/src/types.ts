export interface Env {
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  SUPABASE_URL: string;
  SUPABASE_SERVICE_ROLE_KEY: string;
  LLM_API_KEY?: string;
  LLM_MODEL?: string;
  LLM_API_URL?: string;
  LLM_MAX_TOKENS?: string;
  LLM_DAILY_USER_CALLS?: string;
  LLM_DAILY_GLOBAL_CALLS?: string;
  LLM_DAILY_TOKEN_BUDGET?: string;
  ALLOWED_TELEGRAM_CHAT_IDS?: string;
  INBOX_BATCH_SIZE?: string;
}

export interface TelegramTextUpdate {
  update_id: number;
  message: {
    message_id: number;
    date: number;
    from: { id: number; is_bot: boolean };
    chat: { id: number; type: string };
    text: string;
  };
}

export interface InboxMessage {
  update_id: number;
  telegram_user_id: number;
  chat_id: number;
  message_text: string;
  attempts: number;
  lease_token: string;
  response_text?: string | null;
}

export interface InboxRepository {
  enqueue(update: TelegramTextUpdate): Promise<"inserted" | "duplicate">;
  claim(limit: number): Promise<InboxMessage[]>;
  storeReply(updateId: number, leaseToken: string, text: string): Promise<void>;
  markSent(updateId: number, leaseToken: string): Promise<void>;
  markFailure(updateId: number, leaseToken: string, message: string, attempts: number): Promise<void>;
}

export interface TelegramSender {
  sendMessage(chatId: number, text: string): Promise<void>;
}

export interface Assistant {
  respond(telegramUserId: number, message: string, updateId: number, context?: ConversationState | null, chatId?: number): Promise<AssistantResponse>;
}

export interface ConversationState {
  lastIntent?: string | null;
  lastSearchFilters?: Record<string, unknown> | null;
  activeSubscriptionId?: number | null;
}

export interface AssistantResponse {
  text: string;
  usage?: TokenUsage;
  context?: ConversationState;
}

export interface ConversationStore {
  load(telegramUserId: number, chatId: number): Promise<ConversationState | null>;
  save(telegramUserId: number, chatId: number, context: ConversationState): Promise<void>;
}

export interface TokenUsage {
  inputTokens?: number;
  outputTokens?: number;
}

export interface RuntimeDeps {
  repo: InboxRepository;
  telegram: TelegramSender;
  webhookSecret: string;
  inboxBatchSize: number;
  assistant?: Assistant;
  conversations?: ConversationStore;
  allowedChatIds?: ReadonlySet<number>;
}

export interface ScheduledController {
  scheduledTime: number;
  cron: string;
}

export interface ExecutionContextLike {
  waitUntil(promise: Promise<unknown>): void;
}
