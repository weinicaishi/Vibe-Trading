import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const sdk = vi.hoisted(() => ({
  createAuth0Client: vi.fn(),
}));

vi.mock("@auth0/auth0-spa-js", () => ({
  createAuth0Client: sdk.createAuth0Client,
}));

import {
  beginMarketMorningLogin,
  clearMarketMorningAuthLifecycleAdapters,
  clearMarketMorningBearerTokenProviders,
  endMarketMorningSession,
  hasMarketMorningAuthLifecycleAdapter,
  initializeMarketMorningAuth,
  marketMorningAuthHeaders,
} from "@/lib/marketMorningAuth";
import { configureMarketMorningAuth0 } from "@/lib/marketMorningAuth0";

const ENVIRONMENT = {
  VITE_MARKET_MORNING_AUTH_PROVIDER: "auth0",
  VITE_MARKET_MORNING_AUTH0_DOMAIN: "market-morning.jp.auth0.com",
  VITE_MARKET_MORNING_AUTH0_AUDIENCE: "https://api.market-morning.example",
  VITE_MARKET_MORNING_AUTH0_PRODUCT_CLIENT_ID: "product_client_1234567890",
  VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID: "operator_client_123456789",
};

function client(overrides: Record<string, unknown> = {}) {
  return {
    getTokenSilently: vi.fn().mockResolvedValue("access-token"),
    handleRedirectCallback: vi.fn().mockResolvedValue({
      appState: { returnTo: "/market-morning" },
      response_type: "code",
    }),
    isAuthenticated: vi.fn().mockResolvedValue(true),
    loginWithRedirect: vi.fn().mockResolvedValue(undefined),
    logout: vi.fn().mockResolvedValue(undefined),
    revokeRefreshToken: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

describe("Market Morning Auth0 frontend deployment adapter", () => {
  beforeEach(() => {
    sdk.createAuth0Client.mockReset();
    clearMarketMorningAuthLifecycleAdapters();
    clearMarketMorningBearerTokenProviders();
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState(null, "", "/");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
  });

  afterEach(() => {
    clearMarketMorningAuthLifecycleAdapters();
    clearMarketMorningBearerTokenProviders();
    localStorage.clear();
    sessionStorage.clear();
    window.history.replaceState(null, "", "/");
    vi.unstubAllGlobals();
  });

  it("registers isolated product and operator clients with memory-only token caches", async () => {
    const product = client({ getTokenSilently: vi.fn().mockResolvedValue("product-token") });
    const operator = client({ getTokenSilently: vi.fn().mockResolvedValue("operator-token") });
    sdk.createAuth0Client
      .mockResolvedValueOnce(product)
      .mockResolvedValueOnce(operator);

    expect(configureMarketMorningAuth0(ENVIRONMENT)).toBe("configured");
    await initializeMarketMorningAuth("product");
    await initializeMarketMorningAuth("operator");

    expect(sdk.createAuth0Client).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({
        cacheLocation: "memory",
        clientId: ENVIRONMENT.VITE_MARKET_MORNING_AUTH0_PRODUCT_CLIENT_ID,
        domain: ENVIRONMENT.VITE_MARKET_MORNING_AUTH0_DOMAIN,
        refreshTokenMode: "offline",
        useRefreshTokens: true,
        useRefreshTokensFallback: false,
      }),
    );
    expect(sdk.createAuth0Client).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        clientId: ENVIRONMENT.VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID,
      }),
    );
    await expect(marketMorningAuthHeaders("product")).resolves.toEqual({
      Authorization: "Bearer product-token",
    });
    await expect(marketMorningAuthHeaders("operator")).resolves.toEqual({
      Authorization: "Bearer operator-token",
    });
  });

  it("uses PKCE redirect state and provider logout without putting tokens in URLs", async () => {
    const product = client({
      revokeRefreshToken: vi.fn().mockRejectedValue(new Error("provider unavailable")),
    });
    sdk.createAuth0Client.mockResolvedValue(product);
    configureMarketMorningAuth0(ENVIRONMENT);
    await initializeMarketMorningAuth("product");

    await beginMarketMorningLogin("product", "/market-morning/app/watchlist");
    await endMarketMorningSession("product", "/");

    expect(product.loginWithRedirect).toHaveBeenCalledWith({
      appState: { returnTo: "/market-morning/app/watchlist" },
      authorizationParams: {
        audience: ENVIRONMENT.VITE_MARKET_MORNING_AUTH0_AUDIENCE,
        redirect_uri: `${window.location.origin}/market-morning`,
        scope: "openid profile",
        ui_locales: "ja",
      },
    });
    expect(product.revokeRefreshToken).toHaveBeenCalledWith({
      audience: ENVIRONMENT.VITE_MARKET_MORNING_AUTH0_AUDIENCE,
    });
    expect(fetch).toHaveBeenCalledWith(
      "/market-morning/auth/session",
      expect.objectContaining({
        headers: { Authorization: "Bearer access-token" },
        method: "DELETE",
      }),
    );
    expect(product.logout).toHaveBeenCalledWith({
      logoutParams: { returnTo: `${window.location.origin}/` },
    });
    expect(window.location.href).not.toContain("access-token");
  });

  it("scrubs an Auth0 error callback before returning a generic failure", async () => {
    const product = client({
      handleRedirectCallback: vi.fn().mockRejectedValue({
        error: "access_denied",
        error_description: "private provider detail",
      }),
    });
    sdk.createAuth0Client.mockResolvedValue(product);
    window.history.replaceState(
      null,
      "",
      "/market-morning?error=access_denied&error_description=private&state=opaque-state",
    );
    configureMarketMorningAuth0(ENVIRONMENT);

    await expect(initializeMarketMorningAuth("product")).rejects.toThrow(
      "Market Morning authentication is unavailable",
    );
    expect(window.location.pathname).toBe("/market-morning");
    expect(window.location.search).toBe("");
    expect(document.body.textContent).not.toContain("private provider detail");
  });

  it("handles only a complete callback response and restores a validated return path", async () => {
    const product = client({
      handleRedirectCallback: vi.fn().mockResolvedValue({
        appState: { returnTo: "/market-morning/app/settings?from=login" },
        response_type: "code",
      }),
    });
    sdk.createAuth0Client.mockResolvedValue(product);
    window.history.replaceState(
      null,
      "",
      "/market-morning?code=authorization-code&state=opaque-state",
    );
    configureMarketMorningAuth0(ENVIRONMENT);

    await initializeMarketMorningAuth("product");

    expect(product.handleRedirectCallback).toHaveBeenCalledOnce();
    expect(window.location.pathname).toBe("/market-morning/app/settings");
    expect(window.location.search).toBe("?from=login");
    expect(window.location.href).not.toContain("authorization-code");
    expect(window.location.href).not.toContain("opaque-state");
  });

  it("fails closed for malformed callbacks and unsafe app-state destinations", async () => {
    const malformed = client();
    sdk.createAuth0Client.mockResolvedValue(malformed);
    window.history.replaceState(null, "", "/market-morning?state=opaque-state");
    configureMarketMorningAuth0(ENVIRONMENT);

    await expect(initializeMarketMorningAuth("product")).rejects.toThrow(
      "Market Morning authentication is unavailable",
    );
    expect(malformed.handleRedirectCallback).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/market-morning");
    expect(window.location.search).toBe("");

    clearMarketMorningAuthLifecycleAdapters();
    const unsafe = client({
      handleRedirectCallback: vi.fn().mockResolvedValue({
        appState: { returnTo: "https://attacker.example/" },
        response_type: "code",
      }),
    });
    sdk.createAuth0Client.mockResolvedValue(unsafe);
    window.history.replaceState(
      null,
      "",
      "/market-morning?code=authorization-code&state=opaque-state",
    );
    configureMarketMorningAuth0(ENVIRONMENT);

    await expect(initializeMarketMorningAuth("product")).rejects.toThrow(
      "Market Morning authentication is unavailable",
    );
    expect(window.location.pathname).toBe("/market-morning");
    expect(window.location.search).toBe("");
    expect(window.location.href).not.toContain("authorization-code");
    expect(window.location.href).not.toContain("opaque-state");
  });

  it("maps expired interactive sessions to no bearer and surfaces infrastructure errors", async () => {
    const expired = client({
      getTokenSilently: vi.fn().mockRejectedValue({ error: "missing_refresh_token" }),
    });
    sdk.createAuth0Client.mockResolvedValue(expired);
    configureMarketMorningAuth0(ENVIRONMENT);
    await expect(marketMorningAuthHeaders("product")).resolves.toEqual({});

    clearMarketMorningAuthLifecycleAdapters();
    const unavailable = client({
      getTokenSilently: vi.fn().mockRejectedValue(new Error("network detail")),
    });
    sdk.createAuth0Client.mockResolvedValue(unavailable);
    configureMarketMorningAuth0(ENVIRONMENT);
    await expect(marketMorningAuthHeaders("product")).rejects.toThrow(
      "Market Morning authentication is unavailable",
    );
  });

  it("fails closed on unknown providers or incomplete and unsafe Auth0 settings", async () => {
    for (const environment of [
      { ...ENVIRONMENT, VITE_MARKET_MORNING_AUTH_PROVIDER: "unknown" },
      { ...ENVIRONMENT, VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID: "" },
      { ...ENVIRONMENT, VITE_MARKET_MORNING_AUTH0_DOMAIN: "http://localhost:8080" },
      { ...ENVIRONMENT, VITE_MARKET_MORNING_AUTH0_AUDIENCE: "audience with spaces" },
    ]) {
      clearMarketMorningAuthLifecycleAdapters();
      expect(configureMarketMorningAuth0(environment)).toBe("invalid");
      expect(hasMarketMorningAuthLifecycleAdapter("product")).toBe(true);
      expect(hasMarketMorningAuthLifecycleAdapter("operator")).toBe(true);
      await expect(initializeMarketMorningAuth("product")).rejects.toThrow(
        "Market Morning authentication is unavailable",
      );
    }
    expect(sdk.createAuth0Client).not.toHaveBeenCalled();
  });

  it("does nothing when no frontend auth provider is selected", () => {
    expect(configureMarketMorningAuth0({})).toBe("disabled");
    expect(hasMarketMorningAuthLifecycleAdapter("product")).toBe(false);
    expect(hasMarketMorningAuthLifecycleAdapter("operator")).toBe(false);
  });
});
