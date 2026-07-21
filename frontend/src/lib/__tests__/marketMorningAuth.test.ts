import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  beginMarketMorningLogin,
  clearMarketMorningAuthLifecycleAdapters,
  clearMarketMorningBearerTokenProviders,
  endMarketMorningSession,
  hasMarketMorningAuthLifecycleAdapter,
  initializeMarketMorningAuth,
  marketMorningAuthHeaders,
  MarketMorningFrontendAuthError,
  setMarketMorningAuthLifecycleAdapter,
  setMarketMorningBearerTokenProvider,
} from "@/lib/marketMorningAuth";

describe("Market Morning frontend authentication port", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    clearMarketMorningAuthLifecycleAdapters();
    clearMarketMorningBearerTokenProviders();
  });

  afterEach(() => {
    clearMarketMorningBearerTokenProviders();
    clearMarketMorningAuthLifecycleAdapters();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("does not reuse the Vibe API key for product requests", async () => {
    localStorage.setItem("vibe_trading_api_auth_key", "internal-key");

    await expect(marketMorningAuthHeaders("product")).resolves.toEqual({});
  });

  it("keeps the legacy API key only as an operator fallback", async () => {
    localStorage.setItem("vibe_trading_api_auth_key", "internal-key");

    await expect(marketMorningAuthHeaders("operator")).resolves.toEqual({
      Authorization: "Bearer internal-key",
    });
  });

  it("uses isolated in-memory product and operator token providers", async () => {
    localStorage.setItem("vibe_trading_api_auth_key", "must-not-win");
    setMarketMorningBearerTokenProvider("product", async () => "product-token");
    setMarketMorningBearerTokenProvider("operator", () => "operator-token");

    await expect(marketMorningAuthHeaders("product")).resolves.toEqual({
      Authorization: "Bearer product-token",
    });
    await expect(marketMorningAuthHeaders("operator")).resolves.toEqual({
      Authorization: "Bearer operator-token",
    });
    expect(localStorage.getItem("product-token")).toBeNull();
    expect(localStorage.getItem("operator-token")).toBeNull();
  });

  it("does not fall back to the legacy key once an operator provider is registered", async () => {
    localStorage.setItem("vibe_trading_api_auth_key", "legacy-key");
    setMarketMorningBearerTokenProvider("operator", () => null);

    await expect(marketMorningAuthHeaders("operator")).resolves.toEqual({});
  });

  it.each([" leading-space", "trailing-space ", "line\nbreak", "x".repeat(8193)])(
    "rejects an unsafe token without returning it (%s)",
    async (token) => {
      setMarketMorningBearerTokenProvider("product", () => token);

      await expect(marketMorningAuthHeaders("product")).rejects.toThrow(
        "Market Morning authentication is unavailable",
      );
    },
  );

  it("initializes one lifecycle adapter once before concurrent token requests", async () => {
    let releaseInitialization: (() => void) | undefined;
    const initialize = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          releaseInitialization = resolve;
        }),
    );
    const getAccessToken = vi.fn(() => "product-token");
    setMarketMorningAuthLifecycleAdapter("product", {
      initialize,
      getAccessToken,
      login: vi.fn(),
      logout: vi.fn(),
    });

    const initialization = initializeMarketMorningAuth("product");
    const headers = marketMorningAuthHeaders("product");
    await Promise.resolve();
    expect(initialize).toHaveBeenCalledOnce();
    expect(getAccessToken).not.toHaveBeenCalled();
    releaseInitialization?.();

    await expect(initialization).resolves.toBe("configured");
    await expect(headers).resolves.toEqual({ Authorization: "Bearer product-token" });
    await expect(initializeMarketMorningAuth("product")).resolves.toBe("configured");
    expect(initialize).toHaveBeenCalledOnce();
    expect(hasMarketMorningAuthLifecycleAdapter("product")).toBe(true);
    expect(JSON.stringify(localStorage)).not.toContain("product-token");
    expect(JSON.stringify(sessionStorage)).not.toContain("product-token");
  });

  it("retries a sanitized lifecycle initialization failure", async () => {
    const initialize = vi
      .fn()
      .mockRejectedValueOnce(new Error("provider response with private details"))
      .mockResolvedValueOnce(undefined);
    setMarketMorningAuthLifecycleAdapter("product", {
      initialize,
      getAccessToken: () => "fresh-token",
      login: vi.fn(),
      logout: vi.fn(),
    });

    await expect(initializeMarketMorningAuth("product")).rejects.toEqual(
      new MarketMorningFrontendAuthError(),
    );
    await expect(marketMorningAuthHeaders("product")).resolves.toEqual({
      Authorization: "Bearer fresh-token",
    });
    expect(initialize).toHaveBeenCalledTimes(2);
  });

  it("uses lifecycle login and logout with same-origin fragment-free return paths", async () => {
    const login = vi.fn();
    const logout = vi.fn();
    setMarketMorningAuthLifecycleAdapter("product", {
      initialize: vi.fn(),
      getAccessToken: () => null,
      login,
      logout,
    });

    await beginMarketMorningLogin("product", "/market-morning?from=email");
    await endMarketMorningSession("product", "/");

    expect(login).toHaveBeenCalledWith("/market-morning?from=email");
    expect(logout).toHaveBeenCalledWith("/");
    for (const unsafe of [
      "https://attacker.example/",
      "//attacker.example/",
      "/market-morning#token=secret",
      "/market-morning\\escape",
      "/line\nbreak",
    ]) {
      await expect(beginMarketMorningLogin("product", unsafe)).rejects.toThrow(
        "Market Morning authentication is unavailable",
      );
    }
    expect(login).toHaveBeenCalledOnce();
  });

  it("does not use the legacy operator key after a lifecycle adapter is registered", async () => {
    localStorage.setItem("vibe_trading_api_auth_key", "legacy-key");
    setMarketMorningAuthLifecycleAdapter("operator", {
      initialize: vi.fn(),
      getAccessToken: () => null,
      login: vi.fn(),
      logout: vi.fn(),
    });

    await expect(marketMorningAuthHeaders("operator")).resolves.toEqual({});
  });

  it("rejects incomplete lifecycle adapters and reports unconfigured scopes", async () => {
    expect(() =>
      setMarketMorningAuthLifecycleAdapter(
        "product",
        { initialize: vi.fn() } as never,
      ),
    ).toThrow("Market Morning authentication is unavailable");
    await expect(initializeMarketMorningAuth("product")).resolves.toBe("unconfigured");
    await expect(beginMarketMorningLogin("product", "/market-morning")).rejects.toThrow(
      "Market Morning authentication is unavailable",
    );
  });
});
