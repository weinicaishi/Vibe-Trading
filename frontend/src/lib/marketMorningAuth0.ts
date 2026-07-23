import type { Auth0Client, Auth0ClientOptions } from "@auth0/auth0-spa-js";
import {
  type MarketMorningAuthLifecycleAdapter,
  MarketMorningFrontendAuthError,
  normalizeMarketMorningReturnTo,
  setMarketMorningAuthLifecycleAdapter,
} from "@/lib/marketMorningAuth";

type FrontendEnvironment = Record<string, string | boolean | undefined>;

interface Auth0ScopeConfiguration {
  audience: string;
  callbackPath: string;
  clientId: string;
  defaultReturnTo: string;
  domain: string;
  logoutPath: string;
}

interface Auth0RedirectState {
  returnTo?: unknown;
}

const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/;
const AUTHORIZATION_SCOPE = "openid profile offline_access";
const CLIENT_ID = /^[A-Za-z0-9_-]{8,128}$/;
const IPV4 = /^\d{1,3}(?:\.\d{1,3}){3}$/;

async function createClient(options: Auth0ClientOptions & { refreshTokenMode: "offline" }) {
  const { createAuth0Client } = await import("@auth0/auth0-spa-js");
  return createAuth0Client(options);
}

function value(environment: FrontendEnvironment, key: string): string {
  const current = environment[key];
  return typeof current === "string" ? current.trim() : "";
}

function normalizeDomain(raw: string): string {
  if (!raw || raw.length > 253 || CONTROL_CHARACTERS.test(raw) || /\s/.test(raw)) {
    throw new MarketMorningFrontendAuthError();
  }
  let parsed: URL;
  try {
    parsed = new URL(raw.includes("://") ? raw : `https://${raw}`);
  } catch {
    throw new MarketMorningFrontendAuthError();
  }
  const hostname = parsed.hostname.toLowerCase();
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    (parsed.port && parsed.port !== "443") ||
    (parsed.pathname !== "/" && parsed.pathname !== "") ||
    parsed.search ||
    parsed.hash ||
    !hostname.includes(".") ||
    hostname === "localhost" ||
    hostname.endsWith(".localhost") ||
    IPV4.test(hostname) ||
    hostname.includes(":")
  ) {
    throw new MarketMorningFrontendAuthError();
  }
  return hostname;
}

function configuration(
  environment: FrontendEnvironment,
  scope: "product" | "operator",
): Auth0ScopeConfiguration {
  const clientId = value(
    environment,
    scope === "product"
      ? "VITE_MARKET_MORNING_AUTH0_PRODUCT_CLIENT_ID"
      : "VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID",
  );
  const audience = value(environment, "VITE_MARKET_MORNING_AUTH0_AUDIENCE");
  if (
    !CLIENT_ID.test(clientId) ||
    !audience ||
    audience.length > 512 ||
    audience !== audience.trim() ||
    CONTROL_CHARACTERS.test(audience) ||
    /\s/.test(audience)
  ) {
    throw new MarketMorningFrontendAuthError();
  }
  return {
    audience,
    callbackPath: scope === "product" ? "/market-morning" : "/market-morning-ops",
    clientId,
    defaultReturnTo: scope === "product" ? "/market-morning" : "/market-morning-ops",
    domain: normalizeDomain(value(environment, "VITE_MARKET_MORNING_AUTH0_DOMAIN")),
    logoutPath:
      scope === "product"
        ? "/market-morning/auth/session"
        : "/market-morning/_internal/auth/session",
  };
}

function authErrorCode(error: unknown): string | null {
  if (!error || typeof error !== "object" || !("error" in error)) return null;
  const code = (error as { error?: unknown }).error;
  return typeof code === "string" ? code : null;
}

function authenticationRequired(error: unknown): boolean {
  return [
    "consent_required",
    "interaction_required",
    "invalid_grant",
    "login_required",
    "missing_refresh_token",
  ].includes(authErrorCode(error) ?? "");
}

function reportDevelopmentDiagnostic(code: string): void {
  if (import.meta.env.DEV) console.info(`[market-morning-auth] ${code}`);
}

function callbackResponsePresent(search: string): boolean {
  const query = new URLSearchParams(search);
  const hasState = query.has("state");
  const hasResponse = query.has("code") || query.has("error");
  if (hasState !== hasResponse) throw new MarketMorningFrontendAuthError();
  return hasState && hasResponse;
}

function callbackParametersPresent(search: string): boolean {
  const query = new URLSearchParams(search);
  return query.has("code") || query.has("error") || query.has("state");
}

function replaceBrowserLocation(returnTo: string): void {
  window.history.replaceState(window.history.state, "", returnTo);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

class MarketMorningAuth0Adapter implements MarketMorningAuthLifecycleAdapter {
  private client: Auth0Client | null = null;

  constructor(private readonly config: Auth0ScopeConfiguration) {}

  async initialize(): Promise<void> {
    const callbackUrl = `${window.location.origin}${this.config.callbackPath}`;
    const callbackDetected =
      window.location.pathname === this.config.callbackPath &&
      callbackParametersPresent(window.location.search);
    const options = {
      authorizationParams: {
        audience: this.config.audience,
        redirect_uri: callbackUrl,
        scope: AUTHORIZATION_SCOPE,
      },
      authorizeTimeoutInSeconds: 60,
      cacheLocation: "memory",
      clientId: this.config.clientId,
      domain: this.config.domain,
      httpTimeoutInSeconds: 10,
      refreshTokenMode: "offline" as const,
      useRefreshTokens: true,
      useRefreshTokensFallback: false,
    } satisfies Auth0ClientOptions & { refreshTokenMode: "offline" };
    try {
      const client = await createClient(options);
      this.client = client;
      if (callbackDetected && callbackResponsePresent(window.location.search)) {
        const result = await client.handleRedirectCallback<Auth0RedirectState>();
        const returnTo = normalizeMarketMorningReturnTo(
          typeof result.appState?.returnTo === "string"
            ? result.appState.returnTo
            : this.config.defaultReturnTo,
        );
        replaceBrowserLocation(returnTo);
      }
    } catch {
      this.client = null;
      if (callbackDetected) replaceBrowserLocation(this.config.defaultReturnTo);
      throw new MarketMorningFrontendAuthError();
    }
  }

  async getAccessToken(): Promise<string | null> {
    const client = this.requireClient();
    try {
      if (!(await client.isAuthenticated())) {
        reportDevelopmentDiagnostic("not_authenticated");
        return null;
      }
      return await client.getTokenSilently({
        authorizationParams: {
          audience: this.config.audience,
          scope: AUTHORIZATION_SCOPE,
        },
      });
    } catch (error) {
      if (authenticationRequired(error)) {
        reportDevelopmentDiagnostic(
          `token_unavailable:${authErrorCode(error) ?? "authentication_required"}`,
        );
        return null;
      }
      throw new MarketMorningFrontendAuthError();
    }
  }

  async login(returnTo: string): Promise<void> {
    const destination = normalizeMarketMorningReturnTo(returnTo);
    await this.requireClient().loginWithRedirect<Auth0RedirectState>({
      appState: { returnTo: destination },
      authorizationParams: {
        audience: this.config.audience,
        redirect_uri: `${window.location.origin}${this.config.callbackPath}`,
        scope: AUTHORIZATION_SCOPE,
        ui_locales: "ja",
      },
    });
  }

  async logout(returnTo: string): Promise<void> {
    const destination = normalizeMarketMorningReturnTo(returnTo);
    const client = this.requireClient();
    try {
      const token = await this.getAccessToken();
      if (token) {
        await fetch(this.config.logoutPath, {
          cache: "no-store",
          credentials: "same-origin",
          headers: { Authorization: `Bearer ${token}` },
          method: "DELETE",
        });
      }
    } catch {
      // The provider logout below still removes private UI and SDK state. The
      // backend also enforces a short access-token lifetime as the fail-safe.
    }
    try {
      await client.revokeRefreshToken({ audience: this.config.audience });
    } catch {
      // Provider logout still clears the local SDK cache and Auth0 session. A
      // failed refresh-token revocation is enforced server-side by the backend
      // session validator rather than leaving private UI mounted.
    }
    await client.logout({
      logoutParams: { returnTo: `${window.location.origin}${destination}` },
    });
  }

  private requireClient(): Auth0Client {
    if (!this.client) throw new MarketMorningFrontendAuthError();
    return this.client;
  }
}

function failingAdapter(): MarketMorningAuthLifecycleAdapter {
  const fail = () => {
    throw new MarketMorningFrontendAuthError();
  };
  return {
    initialize: fail,
    getAccessToken: fail,
    login: fail,
    logout: fail,
  };
}

export function configureMarketMorningAuth0(
  environment: FrontendEnvironment = import.meta.env,
): "configured" | "disabled" | "invalid" {
  const provider = value(environment, "VITE_MARKET_MORNING_AUTH_PROVIDER");
  if (!provider) return "disabled";
  if (provider !== "auth0") {
    setMarketMorningAuthLifecycleAdapter("product", failingAdapter());
    setMarketMorningAuthLifecycleAdapter("operator", failingAdapter());
    return "invalid";
  }
  try {
    setMarketMorningAuthLifecycleAdapter(
      "product",
      new MarketMorningAuth0Adapter(configuration(environment, "product")),
    );
    setMarketMorningAuthLifecycleAdapter(
      "operator",
      new MarketMorningAuth0Adapter(configuration(environment, "operator")),
    );
    return "configured";
  } catch {
    setMarketMorningAuthLifecycleAdapter("product", failingAdapter());
    setMarketMorningAuthLifecycleAdapter("operator", failingAdapter());
    return "invalid";
  }
}
