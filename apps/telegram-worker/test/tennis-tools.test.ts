import { afterEach, describe, expect, it, vi } from "vitest";
import { SupabaseTennisTools } from "../src/db/tennis-tools";
import type { ToolContext } from "../src/agent/tools";

const env = { SUPABASE_URL: "https://db.example.test", SUPABASE_SERVICE_ROLE_KEY: "test-service-key" };
const context: ToolContext = { telegramUserId: 900123, chatId: 900123, updateId: 77, toolCallId: "call-77" };

function responseJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function facility(overrides: Record<string, unknown> = {}) {
  return {
    id: 4,
    site_id: 2,
    external_id: "verified-1",
    name: "우장산 테니스장 1코트",
    region: "서울 강서구",
    collection_status: "SUPPORTED",
    booking_url: "https://booking.example.test/court",
    enabled: true,
    ...overrides,
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("Supabase tennis tools", () => {
  it("returns only facilities marked as supported and states the coverage limit", async () => {
    const fetchMock = vi.fn(async () => responseJson([facility(), facility({
      id: 5,
      name: "확인 전 시설",
      collection_status: "UNVERIFIED",
    })]));
    vi.stubGlobal("fetch", fetchMock);
    const result = await new SupabaseTennisTools(env).searchFacilities({ region: "강서구" });
    expect(result).toMatchObject({ ok: true, data: { coverage: expect.any(String), facilities: [{ facility_id: 4 }] } });
  });

  it("does not claim national coverage when a region has no supported facility", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => responseJson([])));
    const result = await new SupabaseTennisTools(env).searchFacilities({ region: "양천구" });
    expect(result).toMatchObject({ ok: false, code: "unsupported" });
  });

  it("filters by local starting time and rejects stale observations", async () => {
    const fresh = new Date().toISOString();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/facilities")) return responseJson([facility()]);
      if (url.pathname.endsWith("/availability_slots")) return responseJson([
        { id: 1, facility_id: 4, court_name: "1번 코트", play_date: "2026-10-24", starts_at: "17:00:00+09:00", ends_at: "18:00:00+09:00", status: "AVAILABLE", booking_url: "https://booking.example.test", observed_at: fresh },
        { id: 2, facility_id: 4, court_name: "1번 코트", play_date: "2026-10-24", starts_at: "19:00:00+09:00", ends_at: "20:00:00+09:00", status: "AVAILABLE", booking_url: "https://booking.example.test", observed_at: fresh },
      ]);
      if (url.pathname.endsWith("/reservation_sites")) return responseJson([{ id: 2, poll_minutes: 60 }]);
      if (url.pathname.endsWith("/collection_runs")) return responseJson([]);
      throw new Error(`Unexpected request path ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const service = new SupabaseTennisTools(env);
    const result = await service.searchAvailability({ facility: "우장산", date: "2026-10-24", after_time: "18:00" });
    expect(result).toMatchObject({ ok: true, data: { slots: [{ id: 2, is_stale: false }] } });

    const staleMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/facilities")) return responseJson([facility()]);
      if (url.pathname.endsWith("/availability_slots")) return responseJson([
        { id: 3, facility_id: 4, court_name: "1번 코트", play_date: "2026-10-24", starts_at: "19:00:00+09:00", ends_at: "20:00:00+09:00", status: "AVAILABLE", booking_url: "https://booking.example.test", observed_at: "2020-01-01T00:00:00Z" },
      ]);
      if (url.pathname.endsWith("/reservation_sites")) return responseJson([{ id: 2, poll_minutes: 60 }]);
      if (url.pathname.endsWith("/collection_runs")) return responseJson([]);
      throw new Error(`Unexpected request path ${url.pathname}`);
    });
    vi.stubGlobal("fetch", staleMock);
    expect(await service.searchAvailability({ facility: "우장산", date: "2026-10-24" })).toEqual({ ok: false, code: "stale" });
  });

  it("requires both a verified schedule and a verified policy source", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/facilities")) return responseJson([facility()]);
      if (url.pathname.endsWith("/reservation_schedules")) return responseJson([
        { facility_id: 4, policy_id: 8, play_date: "2026-10-24", audience: "resident", opens_at: "2026-10-20T09:00:00+09:00", verified: true },
      ]);
      if (url.pathname.endsWith("/reservation_policies")) return responseJson([]);
      throw new Error(`Unexpected request path ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    expect(await new SupabaseTennisTools(env).getOpeningSchedule({ facility: "우장산", date: "2026-10-24" })).toEqual({ ok: false, code: "not_found" });
  });

  it("keeps the previous slot state but refuses a current availability claim after collection failure", async () => {
    const observed = new Date(Date.now() - 60_000).toISOString();
    const failed = new Date(Date.now() - 30_000).toISOString();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/facilities")) return responseJson([facility()]);
      if (url.pathname.endsWith("/availability_slots")) return responseJson([
        { id: 2, facility_id: 4, court_name: "1번 코트", play_date: "2026-10-24", starts_at: "19:00:00+09:00", ends_at: "20:00:00+09:00", status: "AVAILABLE", booking_url: "https://booking.example.test", observed_at: observed },
      ]);
      if (url.pathname.endsWith("/reservation_sites")) return responseJson([{ id: 2, poll_minutes: 60 }]);
      if (url.pathname.endsWith("/collection_runs")) return responseJson([
        { facility_id: 4, status: "ERROR", started_at: failed, requested_dates: ["2026-10-24"], error_code: "PAGE_SELECTION_MISMATCH" },
      ]);
      throw new Error(`Unexpected request path ${url.pathname}`);
    }));
    expect(await new SupabaseTennisTools(env).searchAvailability({ facility: "우장산", date: "2026-10-24" }))
      .toEqual({ ok: false, code: "collection_failed" });
  });

  it.each([false, true])("checks that the verified policy snapshot still matches before exposing an opening schedule (changed=%s)", async (changed) => {
    const rule = { audience: "resident", open_days_before: 4, open_time: "09:00:00", timezone_name: "Asia/Seoul", source_url: "https://booking.example.test/policy" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/facilities")) return responseJson([facility()]);
      if (url.pathname.endsWith("/reservation_schedules")) return responseJson([
        { facility_id: 4, policy_id: 8, play_date: "2026-10-24", audience: "resident", opens_at: "2026-10-20T09:00:00+09:00", verified: true },
      ]);
      if (url.pathname.endsWith("/reservation_policies")) return responseJson([
        { id: 8, ...rule, open_days_before: changed ? 5 : 4, verified: true, last_checked_value: JSON.stringify(rule) },
      ]);
      throw new Error(`Unexpected request path ${url.pathname}`);
    }));
    const result = await new SupabaseTennisTools(env).getOpeningSchedule({ facility: "우장산", date: "2026-10-24" });
    expect(result.ok).toBe(!changed);
    if (changed) expect(result.code).toBe("not_found");
  });

  it("sends mutations through the transactional RPC with verified identity and update id", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/facilities")) return responseJson([facility()]);
      if (url.pathname.endsWith("/telegram_users")) return responseJson([{ id: 19, chat_id: "900123", telegram_user_id: 900123 }]);
      if (url.pathname.endsWith("/user_watches")) return responseJson([
        { id: 31, user_id: 19, criteria: { facility: "우장산" }, monitoring_status: "PENDING_ACTIVATION", enabled: true },
      ]);
      if (url.pathname.endsWith("/rpc/apply_user_watch_mutation")) {
        const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
        expect(payload).toMatchObject({
          p_update_id: 77,
          p_tool_call_id: "call-77",
          p_telegram_user_id: 900123,
          p_chat_id: "900123",
          p_action: "create_subscription",
          p_facility_ids: [4],
        });
        return responseJson({
          ok: true,
          data: { subscription_id: 31, monitoring_status: "PENDING_ACTIVATION", facility_count: 1 },
        });
      }
      throw new Error(`Unexpected request path ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const handlers = new SupabaseTennisTools(env).handlers();
    const result = await handlers.create_subscription!(context, {
      facility: "우장산",
      watch_type: "availability",
      weekdays: [6],
      start_time: "18:00",
      end_time: "22:00",
    });
    expect(result).toMatchObject({ ok: true, data: { monitoring_status: "PENDING_ACTIVATION" } });
  });

  it("loads unexpired context and saves it with a fresh one-day expiry", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/telegram_users")) return responseJson([{ id: 19, chat_id: "900123", telegram_user_id: 900123 }]);
      if (url.pathname.endsWith("/conversation_contexts") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        expect(body).toMatchObject({ user_id: 19, conversation_id: "900123", last_intent: "search_availability" });
        expect(Date.parse(String(body.expires_at))).toBeGreaterThan(Date.now());
        return new Response(null, { status: 201 });
      }
      if (url.pathname.endsWith("/conversation_contexts")) return responseJson([{
        last_intent: "search_availability",
        last_search_filters: { region: "강서구", after_time: "18:00" },
        active_subscription_id: null,
      }]);
      throw new Error(`Unexpected request path ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const store = new SupabaseTennisTools(env);
    await expect(store.load(900123, 900123)).resolves.toEqual({
      lastIntent: "search_availability",
      lastSearchFilters: { region: "강서구", after_time: "18:00" },
      activeSubscriptionId: null,
    });
    await store.save(900123, 900123, { lastIntent: "search_availability", lastSearchFilters: { region: "강서구" } });
  });

  it("cannot update a watch owned by a different Telegram user", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/telegram_users")) return responseJson([{ id: 19, chat_id: "900123", telegram_user_id: 900123 }]);
      if (url.pathname.endsWith("/user_watches")) return responseJson([]);
      if (url.pathname.endsWith("/rpc/apply_user_watch_mutation")) throw new Error("RPC must not be called");
      throw new Error(`Unexpected request path ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const handler = new SupabaseTennisTools(env).handlers().update_subscription!;
    await expect(handler(context, { subscription_id: 991, patch: { start_time: "19:00" } })).resolves.toEqual({
      ok: false,
      code: "not_found",
    });
    expect(new URL(String(fetchMock.mock.calls[1]?.[0])).searchParams.get("user_id")).toBe("eq.19");
  });
});

describe("Telegram identity initialization", () => {
  it("links an old Python user without an INSERT or changing enabled", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith('/conversation_contexts')) return responseJson([]);
      if (init?.method === 'PATCH') {
        expect(url.searchParams.get('telegram_user_id')).toBe('is.null');
        expect(JSON.parse(String(init.body))).toEqual({ telegram_user_id: 900123 });
        return responseJson([{ id: 1, chat_id: '900123', telegram_user_id: 900123 }]);
      }
      expect(init?.method).not.toBe('POST');
      return responseJson(url.searchParams.has('chat_id') ? [{ id: 1, chat_id: '900123', telegram_user_id: null }] : []);
    });
    vi.stubGlobal('fetch', fetchMock);
    expect(await new SupabaseTennisTools(env).load(900123, 900123)).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("supplies required enabled on first INSERT and never merges a concurrent user's flag", async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') {
        expect(JSON.parse(String(init.body))).toEqual({ chat_id: '900123', telegram_user_id: 900123, enabled: true });
        expect(new Headers(init.headers).get('Prefer')).toContain('ignore-duplicates');
        return responseJson([{ id: 1, chat_id: '900123', telegram_user_id: 900123 }]);
      }
      return responseJson([]);
    }));
    expect(await new SupabaseTennisTools(env).load(900123, 900123)).toBeNull();
  });

  it("rejects a chat already bound to a different identity", async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      return responseJson(url.searchParams.has('chat_id') ? [{ id: 1, chat_id: '900123', telegram_user_id: 999 }] : []);
    }));
    await expect(new SupabaseTennisTools(env).load(900123, 900123)).rejects.toThrow('identity conflict');
  });
});
