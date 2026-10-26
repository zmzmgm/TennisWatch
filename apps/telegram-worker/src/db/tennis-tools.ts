import type { ConversationState, ConversationStore, Env } from "../types";
import type { ToolContext, ToolHandler, ToolResult } from "../agent/tools";

interface FacilityRow {
  id: number;
  site_id: number;
  external_id: string;
  name: string;
  region: string | null;
  collection_status: string;
  last_collected_at?: string | null;
  booking_url: string;
  enabled: boolean;
}

interface UserRow {
  id: number;
  chat_id: string;
  telegram_user_id: number;
}

const PRIVATE_CONTEXT_TTL_MS = 24 * 60 * 60 * 1000;
const MAX_ROWS = 500;

function ilikeContains(candidate: string | null | undefined, query: unknown): boolean {
  if (typeof query !== "string" || !query.trim()) return true;
  // 공원 이름과 예약 사이트의 시설명, 띄어쓰기 차이를 함께 허용한다.
  const normalized = (value: string) => value.toLocaleLowerCase("ko-KR")
    .replace(/근린공원/g, "").replace(/[\s()·]/g, "");
  const needle = normalized(query);
  return needle.length > 0 && normalized(candidate ?? "").includes(needle);
}

function hhmm(value: string | null | undefined): string {
  return value?.slice(0, 5) ?? "";
}

function filterFacilities(rows: FacilityRow[], input: Record<string, unknown>): FacilityRow[] {
  return rows.filter((row) =>
    ilikeContains(row.region, input.region) && ilikeContains(row.name, input.facility ?? input.query),
  );
}

export class SupabaseTennisTools implements ConversationStore {
  private readonly restUrl: string;

  constructor(private readonly env: Pick<Env, "SUPABASE_URL" | "SUPABASE_SERVICE_ROLE_KEY">) {
    this.restUrl = `${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1`;
  }

  private headers(extra: Record<string, string> = {}): HeadersInit {
    return {
      apikey: this.env.SUPABASE_SERVICE_ROLE_KEY,
      Authorization: `Bearer ${this.env.SUPABASE_SERVICE_ROLE_KEY}`,
      "Content-Type": "application/json",
      ...extra,
    };
  }

  private url(path: string, params: Record<string, string> = {}): string {
    const url = new URL(`${this.restUrl}/${path}`);
    for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
    return url.toString();
  }

  private async rows<T>(path: string, params: Record<string, string>): Promise<T[]> {
    const response = await fetch(this.url(path, params), { headers: this.headers(), signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error(`Supabase query failed (${response.status})`);
    return (await response.json()) as T[];
  }

  private async post<T>(path: string, params: Record<string, string>, body: unknown, ignoreDuplicates = false): Promise<T[]> {
    const response = await fetch(this.url(path, params), {
      method: "POST",
      signal: AbortSignal.timeout(8000),
      headers: this.headers({ Prefer: `resolution=${ignoreDuplicates ? "ignore" : "merge"}-duplicates,return=representation` }),
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Supabase write failed (${response.status})`);
    return (await response.json()) as T[];
  }

  private async patch<T>(path: string, params: Record<string, string>, body: unknown): Promise<T[]> {
    const response = await fetch(this.url(path, params), {
      method: "PATCH",
      signal: AbortSignal.timeout(8000),
      headers: this.headers({ Prefer: "return=representation" }),
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Supabase update failed (${response.status})`);
    return (await response.json()) as T[];
  }

  private async rpc<T>(name: string, body: unknown): Promise<T> {
    const response = await fetch(`${this.restUrl}/rpc/${name}`, {
      method: "POST",
      signal: AbortSignal.timeout(8000),
      headers: this.headers(),
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`Supabase operation failed (${response.status})`);
    return (await response.json()) as T;
  }

  private async facilities(): Promise<FacilityRow[]> {
    return this.rows<FacilityRow>("facilities", {
      select: "id,site_id,external_id,name,region,collection_status,last_collected_at,booking_url,enabled",
      enabled: "eq.true",
      limit: String(MAX_ROWS),
    });
  }

  private async matching(input: Record<string, unknown>): Promise<{ supported: FacilityRow[]; known: FacilityRow[] }> {
    const all = filterFacilities(await this.facilities(), input);
    return {
      supported: all.filter((facility) => facility.collection_status === "SUPPORTED"),
      known: all,
    };
  }

  handlers(): Partial<Record<string, ToolHandler>> {
    return {
      search_facilities: (_context, input) => this.searchFacilities(input),
      search_availability: (_context, input) => this.searchAvailability(input),
      get_opening_schedule: (_context, input) => this.getOpeningSchedule(input),
      create_subscription: (context, input) => this.mutate(context, "create_subscription", input),
      update_subscription: (context, input) => this.mutate(context, "update_subscription", input),
      delete_subscription: (context, input) => this.mutate(context, "delete_subscription", input),
      list_subscriptions: (context) => this.listSubscriptions(context),
      set_daily_report: (context, input) => this.dailyReport(context, "set", input),
      get_daily_report: (context) => this.dailyReport(context, "get", {}),
      disable_daily_report: (context) => this.dailyReport(context, "disable", {}),
    };
  }

  async searchFacilities(input: Record<string, unknown>): Promise<ToolResult> {
    const { supported, known } = await this.matching(input);
    if (supported.length === 0 && (known.length > 0 || input.region !== undefined)) return { ok: false, code: "unsupported" };
    return {
      ok: true,
      data: {
        coverage: "어댑터가 등록된 시설입니다. 실제 수집 성공 여부는 last_successful_collection_at을 확인하세요.",
        facilities: supported.map(({ id, name, region, booking_url, last_collected_at }) => ({
          facility_id: id,
          name,
          region,
          booking_url,
          collection_status: "SUPPORTED",
          last_successful_collection_at: last_collected_at ?? null,
        })),
      },
    };
  }

  async searchAvailability(input: Record<string, unknown>): Promise<ToolResult> {
    const { supported, known } = await this.matching(input);
    if (supported.length === 0) return { ok: false, code: "unsupported" };
    const ids = supported.map((facility) => facility.id);
    const params: Record<string, string> = {
      select: "id,facility_id,court_name,play_date,starts_at,ends_at,status,booking_url,observed_at",
      facility_id: `in.(${ids.join(",")})`,
      play_date: `eq.${String(input.date)}`,
      order: "starts_at.asc",
      limit: String(MAX_ROWS),
    };
    const slots = await this.rows<{
      id: number;
      facility_id: number;
      court_name: string;
      play_date: string;
      starts_at: string;
      ends_at: string;
      status: string;
      booking_url: string;
      observed_at: string | null;
    }>("availability_slots", params);
    const siteIds = [...new Set(supported.map((f) => f.site_id))];
    const sites = await this.rows<{ id: number; poll_minutes: number }>("reservation_sites", {
      select: "id,poll_minutes",
      id: `in.(${siteIds.join(",")})`,
    });
    const pollMinutesBySite = new Map(sites.map((site) => [site.id, site.poll_minutes]));
    const runs = await this.rows<{
      facility_id: number;
      status: string;
      started_at: string;
      requested_dates: string[] | null;
      error_code: string | null;
    }>("collection_runs", {
      select: "facility_id,status,started_at,requested_dates,error_code",
      facility_id: `in.(${ids.join(",")})`,
      order: "started_at.desc",
      limit: String(MAX_ROWS),
    });
    const latestRunByFacility = new Map<number, (typeof runs)[number]>();
    for (const run of runs) {
      if (run.requested_dates?.includes(String(input.date)) && !latestRunByFacility.has(run.facility_id)) {
        latestRunByFacility.set(run.facility_id, run);
      }
    }
    const facilityById = new Map(supported.map((facility) => [facility.id, facility]));
    const now = Date.now();
    const classified = slots.map((slot) => {
      const facility = facilityById.get(slot.facility_id)!;
      const observedAtMs = slot.observed_at ? Date.parse(slot.observed_at) : 0;
      const freshnessLimit = Math.max(30, pollMinutesBySite.get(facility.site_id) ?? 60) * 2 * 60_000;
      const lastRun = latestRunByFacility.get(facility.id);
      const collectionFailed = !!lastRun && ["ERROR", "PARTIAL"].includes(lastRun.status) && Date.parse(lastRun.started_at) > observedAtMs;
      const isStale = !Number.isFinite(observedAtMs) || !observedAtMs || observedAtMs > now + 60_000 ||
        now - observedAtMs > freshnessLimit || collectionFailed;
      return {
        ...slot,
        facility_name: facility.name,
        region: facility.region,
        observed_at: slot.observed_at,
        last_checked_at: slot.observed_at,
        is_stale: isStale,
        collection_failed_since_observation: collectionFailed,
        status: slot.status,
      };
    });
    if (classified.length === 0) return { ok: false, code: [...latestRunByFacility.values()].some((run) => ["ERROR", "PARTIAL"].includes(run.status)) ? "collection_failed" : "stale" };
    const inRequestedRange = (slot: (typeof classified)[number]) => {
      const start = hhmm(slot.starts_at);
      return (input.after_time === undefined || start >= String(input.after_time)) &&
        (input.before_time === undefined || start < String(input.before_time));
    };
    const relevant = classified.filter(inRequestedRange);
    const freshRows = relevant.filter((slot) => !slot.is_stale);
    const available = freshRows.filter((slot) => slot.status === "AVAILABLE");
    const missingFacilityData = supported.some((facility) => !classified.some((slot) => slot.facility_id === facility.id));
    if (available.length === 0 && (relevant.some((slot) => slot.is_stale) || missingFacilityData)) return { ok: false,
      code: relevant.some((slot) => slot.collection_failed_since_observation) ? "collection_failed" : "stale" };
    return { ok: true, data: {
      date: input.date,
      facilities: supported.length,
      partial: classified.some((slot) => slot.is_stale) || missingFacilityData,
      slots: available,
    } };
  }

  async getOpeningSchedule(input: Record<string, unknown>): Promise<ToolResult> {
    const { supported, known } = await this.matching({ facility: input.facility });
    if (supported.length === 0) return { ok: false, code: known.length ? "unsupported" : "not_found" };
    const schedules = await this.rows<{
      facility_id: number;
      policy_id: number;
      play_date: string;
      play_date_end: string | null;
      audience: string;
      opens_at: string;
      verified: boolean;
    }>("reservation_schedules", {
      select: "facility_id,policy_id,play_date,play_date_end,audience,opens_at,verified",
      facility_id: `in.(${supported.map((f) => f.id).join(",")})`,
      play_date: `lte.${String(input.date)}`,
      play_date_end: `gte.${String(input.date)}`,
      verified: "eq.true",
      order: "opens_at.asc",
    });
    if (schedules.length === 0) return { ok: false, code: "not_found" };
    const policies = await this.rows<{
      id: number; verified: boolean; source_url: string; audience: string; open_days_before: number;
      open_time: string; timezone_name: string; last_checked_value: string | null;
      rule_type: string; open_weekday: number | null; window_start_days: number | null; window_days: number | null;
      source_digest: string | null; source_checked_at: string | null; source_check_error: string | null;
    }>("reservation_policies", {
      select: "id,verified,source_url,audience,open_days_before,open_time,timezone_name,last_checked_value,rule_type,open_weekday,window_start_days,window_days,source_digest,source_checked_at,source_check_error",
      id: `in.(${[...new Set(schedules.map((schedule) => schedule.policy_id))].join(",")})`,
      verified: "eq.true",
    });
    const policyById = new Map(policies.map((policy) => [policy.id, policy]));
    const facilityById = new Map(supported.map((facility) => [facility.id, facility]));
    const data = schedules.flatMap((schedule) => {
      const policy = policyById.get(schedule.policy_id);
      const facility = facilityById.get(schedule.facility_id);
      if (!policy || !facility) return [];
      try {
        if (!policy.last_checked_value) return [];
        const snapshot = JSON.parse(policy.last_checked_value) as Record<string, unknown>;
        const sourceCheckedMs = policy.source_checked_at ? Date.parse(policy.source_checked_at) : NaN;
        if (policy.source_check_error || (policy.source_digest && (!Number.isFinite(sourceCheckedMs) ||
          Date.now() - sourceCheckedMs > 36 * 60 * 60 * 1000 || sourceCheckedMs > Date.now() + 60_000))) return [];
        if (snapshot.audience !== policy.audience || snapshot.open_days_before !== policy.open_days_before ||
          String(snapshot.open_time).slice(0, 8) !== policy.open_time.slice(0, 8) ||
          snapshot.timezone_name !== policy.timezone_name || snapshot.source_url !== policy.source_url ||
          (snapshot.rule_type ?? "rolling_days") !== policy.rule_type ||
          (snapshot.open_weekday ?? null) !== policy.open_weekday ||
          (snapshot.window_start_days ?? null) !== policy.window_start_days ||
          (snapshot.window_days ?? null) !== policy.window_days ||
          (snapshot.source_digest ?? null) !== policy.source_digest) return [];
      } catch {
        return [];
      }
      return [{
        facility_id: facility.id,
        facility_name: facility.name,
        region: facility.region,
        play_date: schedule.play_date,
        play_date_end: schedule.play_date_end ?? schedule.play_date,
        audience: schedule.audience,
        opens_at: schedule.opens_at,
        verified: true,
        source_url: policy.source_url,
      }];
    });
    return data.length ? { ok: true, data } : { ok: false, code: "not_found" };
  }

  private async ensureUser(telegramUserId: number): Promise<UserRow> {
    const existing = await this.rows<UserRow>("telegram_users", {
      select: "id,chat_id,telegram_user_id",
      telegram_user_id: `eq.${telegramUserId}`,
      limit: "1",
    });
    if (existing[0]) return existing[0];
    // Old Python subscriptions may have a chat ID but no Telegram user ID.
    // Preserve their enabled flag and bind identity only while it is still unset.
    const byChat = await this.rows<UserRow>("telegram_users", {
      select: "id,chat_id,telegram_user_id", chat_id: `eq.${telegramUserId}`, limit: "1",
    });
    if (byChat[0] && byChat[0].telegram_user_id == null) {
      const linked = await this.patch<UserRow>("telegram_users", {
        id: `eq.${byChat[0].id}`, telegram_user_id: "is.null",
      }, { telegram_user_id: telegramUserId });
      if (linked[0]) return linked[0];
    } else if (byChat[0]?.telegram_user_id === telegramUserId) {
      return byChat[0];
    } else if (byChat[0]) {
      throw new Error("Telegram identity conflict");
    }
    if (!byChat[0]) {
      // SQLAlchemy's Python default does not supply a PostgREST INSERT default.
      const inserted = await this.post<UserRow>("telegram_users", { on_conflict: "chat_id" }, {
        chat_id: String(telegramUserId), telegram_user_id: telegramUserId, enabled: true,
      }, true);
      if (inserted[0]) return inserted[0];
    }
    const raced = await this.rows<UserRow>("telegram_users", {
      select: "id,chat_id,telegram_user_id", telegram_user_id: `eq.${telegramUserId}`, limit: "1",
    });
    if (!raced[0] || raced[0].chat_id !== String(telegramUserId)) throw new Error("Telegram user could not be initialized");
    return raced[0];
  }

  private async resolveSupportedFacilities(criteria: Record<string, unknown>): Promise<FacilityRow[]> {
    const { supported, known } = await this.matching(criteria);
    if (supported.length === 0) {
      if (known.length || criteria.region !== undefined) throw new Error("unsupported facility");
      throw new Error("unknown facility");
    }
    return supported;
  }

  private async mutate(context: ToolContext, action: string, input: Record<string, unknown>): Promise<ToolResult> {
    try {
      let facilityIds: number[] | null = null;
      if (action === "create_subscription") {
        const criteria = { ...input };
        delete criteria.watch_type;
        facilityIds = (await this.resolveSupportedFacilities(criteria)).map((facility) => facility.id);
      } else if (action === "update_subscription") {
        const patch = input.patch as Record<string, unknown>;
        const owned = await this.ownedWatch(context.telegramUserId, Number(input.subscription_id));
        if (!owned) return { ok: false, code: "not_found" };
        const mergedCriteria = { ...owned.criteria, ...patch };
        if (patch.play_date !== undefined) delete mergedCriteria.weekdays;
        if (patch.weekdays !== undefined) delete mergedCriteria.play_date;
        if (patch.region !== undefined && patch.facility === undefined) delete mergedCriteria.facility;
        if (patch.facility !== undefined && patch.region === undefined) delete mergedCriteria.region;
        if (typeof mergedCriteria.start_time === "string" && typeof mergedCriteria.end_time === "string" &&
          mergedCriteria.end_time <= mergedCriteria.start_time) return { ok: false, code: "invalid_input" };
        if (patch.facility !== undefined || patch.region !== undefined) {
          facilityIds = (await this.resolveSupportedFacilities(mergedCriteria)).map((facility) => facility.id);
        }
      }

      const args = { ...input };
      if (action === "create_subscription" && (args as Record<string, unknown>).watch_type === "opening" && !(args as Record<string, unknown>).reminders) {
        (args as Record<string, unknown>).reminders = ["day_before", "hour_before", "opened"];
      }
      const result = await this.rpc<ToolResult>("apply_user_watch_mutation", {
        p_update_id: context.updateId,
        p_tool_call_id: context.toolCallId,
        p_telegram_user_id: context.telegramUserId,
        p_chat_id: String(context.chatId),
        p_action: action,
        p_args: args,
        p_facility_ids: facilityIds,
      });
      if (!result.ok) return result;
      const data = result.data as Record<string, unknown> | undefined;
      if (typeof data?.subscription_id !== "number") return { ok: false, code: "failed" };
      const persisted = await this.ownedWatch(context.telegramUserId, data.subscription_id);
      if (!persisted) return { ok: false, code: "failed" };
      return { ok: true, data: { ...data, criteria: persisted.criteria,
        monitoring_status: persisted.monitoring_status, enabled: persisted.enabled } };
    } catch (error) {
      if (error instanceof Error && error.message.includes("unsupported facility")) return { ok: false, code: "unsupported" };
      if (error instanceof Error && error.message.includes("unknown facility")) return { ok: false, code: "not_found" };
      throw error;
    }
  }

  private async ownedWatch(telegramUserId: number, id: number): Promise<{
    id: number; user_id: number; criteria: Record<string, unknown>; monitoring_status: string; enabled: boolean;
  } | null> {
    const user = await this.ensureUser(telegramUserId);
    const rows = await this.rows<{ id: number; user_id: number; criteria: Record<string, unknown>; monitoring_status: string; enabled: boolean }>("user_watches", {
      select: "id,user_id,criteria,monitoring_status,enabled",
      id: `eq.${id}`,
      user_id: `eq.${user.id}`,
      limit: "1",
    });
    return rows[0] ?? null;
  }

  async listSubscriptions(context: ToolContext): Promise<ToolResult> {
    const user = await this.ensureUser(context.telegramUserId);
    const watches = await this.rows<{
      id: number;
      watch_type: string;
      criteria: Record<string, unknown>;
      enabled: boolean;
      monitoring_status: string;
      expires_at: string | null;
      updated_at: string;
    }>("user_watches", {
      select: "id,watch_type,criteria,enabled,monitoring_status,expires_at,updated_at",
      user_id: `eq.${user.id}`,
      enabled: "eq.true",
      or: `(expires_at.is.null,expires_at.gt.${new Date().toISOString()})`,
      order: "updated_at.desc",
      limit: "100",
    });
    if (watches.length === 0) return { ok: true, data: [] };
    const links = await this.rows<{ user_watch_id: number; facility_id: number }>("user_watch_facilities", {
      select: "user_watch_id,facility_id",
      user_watch_id: `in.(${watches.map((watch) => watch.id).join(",")})`,
    });
    const ids = [...new Set(links.map((link) => link.facility_id))];
    const facilities = ids.length ? await this.rows<FacilityRow>("facilities", {
      select: "id,site_id,external_id,name,region,collection_status,booking_url,enabled",
      id: `in.(${ids.join(",")})`,
    }) : [];
    const facilityById = new Map(facilities.map((facility) => [facility.id, facility]));
    const facilitiesByWatch = new Map<number, FacilityRow[]>();
    for (const link of links) {
      const facility = facilityById.get(link.facility_id);
      if (!facility) continue;
      const list = facilitiesByWatch.get(link.user_watch_id) ?? [];
      list.push(facility);
      facilitiesByWatch.set(link.user_watch_id, list);
    }
    return {
      ok: true,
      data: watches.map((watch) => ({
        subscription_id: watch.id,
        watch_type: watch.watch_type,
        criteria: watch.criteria,
        enabled: watch.enabled,
        monitoring_status: watch.monitoring_status,
        expires_at: watch.expires_at,
        facilities: (facilitiesByWatch.get(watch.id) ?? []).map(({ id, name, region, collection_status }) => ({ id, name, region, collection_status })),
      })),
    };
  }

  private async dailyReport(context: ToolContext, action: string, input: Record<string, unknown>): Promise<ToolResult> {
    try {
      const minute = typeof input.local_time === "string"
        ? Number(input.local_time.slice(0, 2))*60+Number(input.local_time.slice(3, 5)) : null;
      return await this.rpc<ToolResult>("manage_daily_report", {
        p_update_id: context.updateId, p_user_id: context.telegramUserId,
        p_chat_id: String(context.chatId), p_action: action, p_minute: minute,
      });
    } catch {
      return { ok: false, code: "failed" };
    }
  }

  async load(telegramUserId: number, chatId: number): Promise<ConversationState | null> {
    const user = await this.ensureUser(telegramUserId);
    const rows = await this.rows<{
      last_intent: string | null;
      last_search_filters: Record<string, unknown> | null;
      active_subscription_id: number | null;
    }>("conversation_contexts", {
      select: "last_intent,last_search_filters,active_subscription_id",
      user_id: `eq.${user.id}`,
      conversation_id: `eq.${chatId}`,
      expires_at: `gt.${new Date().toISOString()}`,
      limit: "1",
    });
    const context = rows[0];
    return context ? {
      lastIntent: context.last_intent,
      lastSearchFilters: context.last_search_filters,
      activeSubscriptionId: context.active_subscription_id,
    } : null;
  }

  async save(telegramUserId: number, chatId: number, context: ConversationState): Promise<void> {
    const user = await this.ensureUser(telegramUserId);
    const filters = context.lastSearchFilters ? JSON.stringify(context.lastSearchFilters).slice(0, 2000) : null;
    const response = await fetch(this.url("conversation_contexts", { on_conflict: "user_id,conversation_id" }), {
      method: "POST",
      signal: AbortSignal.timeout(8000),
      headers: this.headers({ Prefer: "resolution=merge-duplicates,return=minimal" }),
      body: JSON.stringify({
        user_id: user.id,
        conversation_id: String(chatId),
        last_intent: context.lastIntent ?? null,
        last_search_filters: filters ? JSON.parse(filters) : null,
        active_subscription_id: context.activeSubscriptionId ?? null,
        updated_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + PRIVATE_CONTEXT_TTL_MS).toISOString(),
      }),
    });
    if (!response.ok) throw new Error(`Supabase conversation write failed (${response.status})`);
  }
}
