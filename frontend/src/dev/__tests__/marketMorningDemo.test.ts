import { describe, expect, it } from "vitest";
import {
  createMarketMorningDemoState,
  handleMarketMorningDemoRequest,
  isMarketMorningDemoEnabled,
  MARKET_MORNING_DEV_API_PROXY_PATTERN,
} from "@/dev/marketMorningDemo";

function request(method: string, url: string, body?: unknown) {
  const state = createMarketMorningDemoState();
  return {
    state,
    send: (nextMethod: string, nextUrl: string, nextBody?: unknown) =>
      handleMarketMorningDemoRequest(state, {
        method: nextMethod,
        url: nextUrl,
        body: nextBody,
      }),
    response: handleMarketMorningDemoRequest(state, { method, url, body }),
  };
}

describe("Market Morning local browser demo", () => {
  it("cannot be enabled outside the Vite development mode", () => {
    expect(isMarketMorningDemoEnabled("development", "true")).toBe(true);
    expect(isMarketMorningDemoEnabled("production", "true")).toBe(false);
    expect(isMarketMorningDemoEnabled("staging", "true")).toBe(false);
    expect(isMarketMorningDemoEnabled("development", "false")).toBe(false);
    expect(isMarketMorningDemoEnabled("development", undefined)).toBe(false);
  });

  it("serves a clearly labeled synthetic edition and three watched issuers", () => {
    const edition = request("GET", "/market-morning/edition/today").response;
    const watchlist = request("GET", "/market-morning/watchlist").response;

    expect(edition?.status).toBe(200);
    expect(edition?.body).toMatchObject({
      data_mode: "synthetic_fixture",
      fixture_version: "local-browser-demo-v1",
    });
    expect(watchlist?.body).toMatchObject({ active_count: 3, limit: 10 });
  });

  it("keeps watchlist search and mutation state for one dev-server process", () => {
    const demo = request("GET", "/market-morning/watchlist");
    const search = demo.send(
      "GET",
      "/market-morning/issuers/search?q=9432&limit=10",
    );
    const issuer = (
      search?.body as { items: Array<{ issuer_id: string }> }
    ).items[0];
    const added = demo.send("POST", "/market-morning/watchlist", {
      issuer_id: issuer.issuer_id,
    });
    const current = demo.send("GET", "/market-morning/watchlist");
    const removed = demo.send(
      "DELETE",
      `/market-morning/watchlist/${issuer.issuer_id}`,
    );

    expect(added?.body).toMatchObject({ status: "added", active_count: 4 });
    expect(current?.body).toMatchObject({ active_count: 4 });
    expect(removed?.body).toMatchObject({ status: "removed", active_count: 3 });
  });

  it("persists settings and private research notes only in demo memory", () => {
    const demo = request("GET", "/market-morning/settings");
    const settings = demo.send("PATCH", "/market-morning/settings", {
      email_opt_in: true,
    });
    const note = demo.send(
      "PUT",
      "/market-morning/issuers/22222222-2222-4222-8222-222222222222/note",
      { text: "次回の開示を確認する。" },
    );
    const research = demo.send(
      "GET",
      "/market-morning/issuers/22222222-2222-4222-8222-222222222222/research?limit=50",
    );

    expect(settings?.body).toMatchObject({ email_opt_in: true });
    expect(note?.body).toMatchObject({
      status: "saved",
      note: { text: "次回の開示を確認する。" },
    });
    expect(research?.body).toMatchObject({
      note: { text: "次回の開示を確認する。" },
    });
  });

  it("does not intercept unknown product or internal operations routes", () => {
    expect(request("GET", "/market-morning/unknown").response).toBeNull();
    expect(
      request(
        "GET",
        "/market-morning/_internal/operations/summary?hours=24",
    ).response,
    ).toBeNull();
  });

  it("keeps every browser-facing Market Morning API route behind the dev proxy", () => {
    const pattern = new RegExp(MARKET_MORNING_DEV_API_PROXY_PATTERN);

    expect(pattern.test("/market-morning/delivery-links/redeem")).toBe(true);
    expect(
      pattern.test(
        "/market-morning/editions/edition-1/events/event-1/report",
      ),
    ).toBe(true);
    expect(pattern.test("/market-morning/edition/today")).toBe(true);
    expect(pattern.test("/market-morning/navigation-only")).toBe(false);
  });
});
