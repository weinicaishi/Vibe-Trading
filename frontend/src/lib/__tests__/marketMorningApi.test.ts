import { afterEach, describe, expect, it, vi } from "vitest";
import {
  MarketMorningApiError,
  classifyMarketMorningAccessError,
  marketMorningApi,
} from "@/lib/marketMorningApi";
import {
  clearMarketMorningBearerTokenProviders,
  setMarketMorningBearerTokenProvider,
} from "@/lib/marketMorningAuth";

afterEach(() => {
  clearMarketMorningBearerTokenProviders();
  vi.unstubAllGlobals();
});

describe("Market Morning API", () => {
  it("uses same-origin product auth without reusing the Vibe API key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          timezone: "Asia/Tokyo",
          email_opt_in: false,
          risk_disclosure: {
            consent_type: "risk_disclosure",
            accepted: false,
            consent_version: null,
            accepted_at: null,
            revoked_at: null,
          },
          data_disclosure: {
            consent_type: "data_disclosure",
            accepted: false,
            consent_version: null,
            accepted_at: null,
            revoked_at: null,
          },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    window.localStorage.setItem("vibe_trading_api_auth_key", "must-not-be-reused");

    await marketMorningApi.getSettings();

    expect(fetchMock).toHaveBeenCalledWith(
      "/market-morning/settings",
      expect.objectContaining({ credentials: "same-origin" }),
    );
    const options = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = new Headers(options.headers);
    expect(headers.get("Authorization")).toBeNull();
    expect(headers.get("Accept")).toBe("application/json");
  });

  it("injects a deployment-owned product bearer without persisting it", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ timezone: "Asia/Tokyo" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    setMarketMorningBearerTokenProvider("product", async () => "oidc-product-token");

    await marketMorningApi.getSettings();

    const options = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(options.headers).get("Authorization")).toBe(
      "Bearer oidc-product-token",
    );
    expect(JSON.stringify(window.localStorage)).not.toContain("oidc-product-token");
  });

  it("exports account data through the authenticated product route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ schema_version: 1, generated_at: "2026-07-21T01:30:00Z", data: {} }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await marketMorningApi.exportAccountData();

    expect(fetchMock.mock.calls[0][0]).toBe("/market-morning/account-data-export");
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("user_id");
  });

  it("encodes issuer search text in the query string", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ query: "", normalized_query: "", items: [], no_match_guidance: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await marketMorningApi.searchIssuers("トヨタ 自動車");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/market-morning/issuers/search?q=%E3%83%88%E3%83%A8%E3%82%BF%20%E8%87%AA%E5%8B%95%E8%BB%8A&limit=10",
    );
  });

  it("loads today's edition through the product-authenticated endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "not_published",
          data_mode: "unavailable",
          edition_id: null,
          reason_code: "edition_repository_not_configured",
          fixture_version: null,
          edition: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await marketMorningApi.getTodayEdition();

    expect(fetchMock.mock.calls[0][0]).toBe("/market-morning/edition/today");
    expect(fetchMock.mock.calls[0][1]).toEqual(
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("redeems an email delivery token in the authenticated request body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "accepted",
          edition_date: "2026-07-21",
          destination_path: "/market-morning",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const token = "A".repeat(43);

    await marketMorningApi.redeemDeliveryLink(token);

    expect(fetchMock.mock.calls[0]).toEqual([
      "/market-morning/delivery-links/redeem",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: JSON.stringify({ token }),
      }),
    ]);
    expect(String(fetchMock.mock.calls[0][0])).not.toContain(token);
  });

  it("loads and updates private edition event state", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const editionId = "22222222-2222-4222-8222-222222222222";
    const eventId = "33333333-3333-4333-8333-333333333333";

    await marketMorningApi.getEditionEventStates(editionId);
    await marketMorningApi.updateEditionEventState(editionId, eventId, "later");

    expect(fetchMock.mock.calls[0][0]).toBe(
      `/market-morning/editions/${editionId}/event-states`,
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      `/market-morning/editions/${editionId}/events/${eventId}/state`,
    );
    expect(fetchMock.mock.calls[1][1]).toEqual(
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ state: "later" }) }),
    );
  });

  it("records an exact source open without sending the source URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "recorded",
          source_open_id: "66666666-6666-4666-8666-666666666666",
          original_url: "https://example.jp/source.pdf",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const editionId = "22222222-2222-4222-8222-222222222222";
    const payload = {
      event_id: "33333333-3333-4333-8333-333333333333",
      provider: "tdnet",
      document_id: "TD-7203-001",
      revision_key: "v1",
      request_id: "77777777-7777-4777-8777-777777777777",
    };

    await marketMorningApi.recordEditionSourceOpen(editionId, payload);

    const [path, options] = fetchMock.mock.calls[0];
    expect(path).toBe(`/market-morning/editions/${editionId}/sources/open`);
    expect(options).toEqual(
      expect.objectContaining({ method: "POST", body: JSON.stringify(payload) }),
    );
    expect(String(options.body)).not.toContain("original_url");
  });

  it("reports one persisted edition event with a fixed reason and no user payload", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "reported",
          report_id: "88888888-8888-4888-8888-888888888888",
          reason_code: "source_mismatch",
          report_status: "pending",
          created_at: "2026-07-21T01:00:00Z",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const editionId = "22222222-2222-4222-8222-222222222222";
    const eventId = "33333333-3333-4333-8333-333333333333";

    await marketMorningApi.reportEditionEvent(
      editionId,
      eventId,
      "source_mismatch",
    );

    const [path, options] = fetchMock.mock.calls[0];
    expect(path).toBe(
      `/market-morning/editions/${editionId}/events/${eventId}/report`,
    );
    expect(options).toEqual(
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: JSON.stringify({ reason_code: "source_mismatch" }),
      }),
    );
    expect(String(options.body)).not.toContain("user_id");
    expect(String(options.body)).not.toContain("original_url");
    expect(String(options.body)).not.toContain("comment");
  });

  it("loads issuer research and saves or clears a private note", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      new Response(JSON.stringify({ events: [], note: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const issuerId = "22222222-2222-4222-8222-222222222222";
    const noteText = "次回の開示を確認する。";

    await marketMorningApi.getIssuerResearch(issuerId);
    await marketMorningApi.updateIssuerNote(issuerId, noteText);
    await marketMorningApi.deleteIssuerNote(issuerId);

    expect(fetchMock.mock.calls[0][0]).toBe(
      `/market-morning/issuers/${issuerId}/research?limit=50`,
    );
    expect(fetchMock.mock.calls[1]).toEqual([
      `/market-morning/issuers/${issuerId}/note`,
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ text: noteText }),
      }),
    ]);
    expect(fetchMock.mock.calls[2]).toEqual([
      `/market-morning/issuers/${issuerId}/note`,
      expect.objectContaining({ method: "DELETE" }),
    ]);
  });

  it("classifies fail-closed authentication and service states", () => {
    expect(
      classifyMarketMorningAccessError(
        new MarketMorningApiError(
          "Market Morning product authentication is not configured",
          503,
        ),
      ),
    ).toBe("authentication_unconfigured");
    expect(classifyMarketMorningAccessError(new MarketMorningApiError("Forbidden", 403))).toBe(
      "unauthenticated",
    );
    expect(classifyMarketMorningAccessError(new MarketMorningApiError("database unavailable", 503))).toBe(
      "unavailable",
    );
  });

  it("rejects an HTML response without including its body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<!doctype html><p>private page</p>", {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
      ),
    );

    await expect(marketMorningApi.getWatchlist()).rejects.toMatchObject({
      name: "MarketMorningApiError",
      status: 200,
      message: expect.not.stringContaining("private page"),
    });
  });
});
