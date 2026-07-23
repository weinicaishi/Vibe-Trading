import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { MARKET_MORNING_DEV_API_PROXY_PATTERN } from "@/dev/marketMorningDemo";

describe("Vite API proxy config", () => {
  const configPath = path.resolve(__dirname, "../../vite.config.ts");
  const config = fs.readFileSync(configPath, "utf8");

  it("proxies channel runtime endpoints", () => {
    expect(config).toContain('"/channels"');
  });

  it("proxies settings endpoints", () => {
    expect(config).toContain('"/settings/llm"');
    expect(config).toContain('"/settings/data-sources"');
  });

  it("proxies the isolated Market Morning product endpoints", () => {
    expect(config).toContain("[MARKET_MORNING_DEV_API_PROXY_PATTERN]");

    const pattern = new RegExp(MARKET_MORNING_DEV_API_PROXY_PATTERN);
    const proxiedPaths = [
      "/market-morning/runtime-config",
      "/market-morning/settings",
      "/market-morning/edition/today",
      "/market-morning/watchlist",
      "/market-morning/watchlist/issuer-id",
      "/market-morning/issuers/search",
      "/market-morning/issuers/issuer-id/research",
      "/market-morning/issuers/issuer-id/note",
      "/market-morning/consents",
      "/market-morning/account-deletion-requests",
      "/market-morning/account-data-export",
      "/market-morning/private-beta/invitations/accept",
      "/market-morning/delivery-links/redeem",
      "/market-morning/auth/session",
      "/market-morning/editions/edition-id/event-states",
      "/market-morning/editions/edition-id/events/event-id/state",
      "/market-morning/editions/edition-id/events/event-id/report",
      "/market-morning/editions/edition-id/sources/open",
      "/market-morning/_internal/operations/summary",
      "/market-morning/_internal/auth/session",
      "/market-morning/_internal/operations/halts/halt-id",
      "/market-morning/_internal/event-briefs/brief-id/review",
      "/market-morning/_internal/private-beta/invites/invite-id",
      "/market-morning/_internal/users/user-id/access",
    ];

    for (const proxiedPath of proxiedPaths) {
      expect(pattern.test(proxiedPath), proxiedPath).toBe(true);
    }
    expect(pattern.test("/market-morning/app/watchlist")).toBe(false);
  });
});
