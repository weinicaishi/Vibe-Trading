import { afterEach, describe, expect, it, vi } from "vitest";
import { isMarketMorningUiEnabled } from "@/lib/marketMorningConfig";
import {
  clearMarketMorningRuntimeConfigForTests,
  getMarketMorningFrontendEnvironment,
  loadMarketMorningRuntimeConfig,
} from "@/lib/marketMorningRuntimeConfig";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Market Morning frontend runtime configuration", () => {
  afterEach(() => {
    clearMarketMorningRuntimeConfigForTests();
    vi.restoreAllMocks();
  });

  it("installs ready public Auth0 values before the UI reads its feature flag", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      jsonResponse({
        schema_version: 1,
        status: "ready",
        feature_enabled: true,
        ui_enabled: true,
        auth: {
          provider: "auth0",
          domain: "tenant.jp.auth0.com",
          audience: "https://api.market-morning.example",
          product_client_id: "product_client_1234567890",
          operator_client_id: "operator_client_123456789",
        },
        blocking_codes: [],
      }),
    );

    await expect(
      loadMarketMorningRuntimeConfig({ fetcher, timeoutMs: 100 }),
    ).resolves.toBe("ready");

    expect(fetcher).toHaveBeenCalledWith(
      "/market-morning/runtime-config",
      expect.objectContaining({
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }),
    );
    expect(getMarketMorningFrontendEnvironment()).toMatchObject({
      VITE_MARKET_MORNING_ENABLED: "true",
      VITE_MARKET_MORNING_AUTH_PROVIDER: "auth0",
      VITE_MARKET_MORNING_AUTH0_DOMAIN: "tenant.jp.auth0.com",
      VITE_MARKET_MORNING_AUTH0_AUDIENCE:
        "https://api.market-morning.example",
      VITE_MARKET_MORNING_AUTH0_PRODUCT_CLIENT_ID:
        "product_client_1234567890",
      VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID:
        "operator_client_123456789",
    });
    expect(isMarketMorningUiEnabled()).toBe(true);
  });

  it.each(["disabled", "misconfigured"] as const)(
    "keeps the UI closed when the runtime endpoint reports %s",
    async (status) => {
      const fetcher = vi.fn().mockResolvedValue(
        jsonResponse({
          schema_version: 1,
          status,
          feature_enabled: status === "misconfigured",
          ui_enabled: false,
          auth: null,
          blocking_codes:
            status === "misconfigured"
              ? ["frontend_auth0_product_client_id_invalid"]
              : [],
        }),
      );

      await expect(
        loadMarketMorningRuntimeConfig({ fetcher, timeoutMs: 100 }),
      ).resolves.toBe(status);

      expect(isMarketMorningUiEnabled()).toBe(false);
      expect(
        getMarketMorningFrontendEnvironment()
          .VITE_MARKET_MORNING_AUTH_PROVIDER,
      ).toBe("");
    },
  );

  it("fails closed in production when the endpoint is unavailable or malformed", async () => {
    const fallbackEnvironment = {
      VITE_MARKET_MORNING_ENABLED: "true",
      VITE_MARKET_MORNING_AUTH_PROVIDER: "auth0",
    };

    await expect(
      loadMarketMorningRuntimeConfig({
        allowBuildTimeFallback: false,
        fallbackEnvironment,
        fetcher: vi.fn().mockRejectedValue(new Error("offline")),
        timeoutMs: 100,
      }),
    ).resolves.toBe("unavailable");
    expect(isMarketMorningUiEnabled()).toBe(false);

    clearMarketMorningRuntimeConfigForTests();
    await expect(
      loadMarketMorningRuntimeConfig({
        allowBuildTimeFallback: false,
        fallbackEnvironment,
        fetcher: vi.fn().mockResolvedValue(jsonResponse({ status: "ready" })),
        timeoutMs: 100,
      }),
    ).resolves.toBe("unavailable");
    expect(isMarketMorningUiEnabled()).toBe(false);
  });

  it("allows an explicit development-only fallback to existing VITE values", async () => {
    const fallbackEnvironment = {
      VITE_MARKET_MORNING_ENABLED: "true",
      VITE_MARKET_MORNING_AUTH_PROVIDER: "auth0",
      VITE_MARKET_MORNING_AUTH0_DOMAIN: "local.jp.auth0.com",
    };

    await expect(
      loadMarketMorningRuntimeConfig({
        allowBuildTimeFallback: true,
        fallbackEnvironment,
        fetcher: vi.fn().mockRejectedValue(new Error("backend absent")),
        timeoutMs: 100,
      }),
    ).resolves.toBe("fallback");

    expect(getMarketMorningFrontendEnvironment()).toBe(fallbackEnvironment);
    expect(isMarketMorningUiEnabled()).toBe(true);

    clearMarketMorningRuntimeConfigForTests();
    await expect(
      loadMarketMorningRuntimeConfig({
        allowBuildTimeFallback: true,
        fallbackEnvironment,
        fetcher: vi.fn().mockResolvedValue(
          jsonResponse({
            schema_version: 1,
            status: "misconfigured",
            feature_enabled: true,
            ui_enabled: false,
            auth: null,
            blocking_codes: ["frontend_auth0_domain_invalid"],
          }),
        ),
        timeoutMs: 100,
      }),
    ).resolves.toBe("fallback");
    expect(isMarketMorningUiEnabled()).toBe(true);
  });
});
