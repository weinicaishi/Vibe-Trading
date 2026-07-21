import { authHeaders } from "@/lib/apiAuth";

export type MarketMorningAuthScope = "product" | "operator";
export type MarketMorningBearerTokenProvider = () =>
  | string
  | null
  | Promise<string | null>;

export interface MarketMorningAuthLifecycleAdapter {
  initialize: () => void | Promise<void>;
  getAccessToken: MarketMorningBearerTokenProvider;
  login: (returnTo: string) => void | Promise<void>;
  logout: (returnTo: string) => void | Promise<void>;
}

const MAX_BEARER_LENGTH = 8192;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/;
const providers: Partial<
  Record<MarketMorningAuthScope, MarketMorningBearerTokenProvider>
> = {};
interface LifecycleEntry {
  adapter: MarketMorningAuthLifecycleAdapter;
  initialized: boolean;
  initialization: Promise<void> | null;
}

const lifecycleEntries: Partial<Record<MarketMorningAuthScope, LifecycleEntry>> = {};

export class MarketMorningFrontendAuthError extends Error {
  constructor() {
    super("Market Morning authentication is unavailable");
    this.name = "MarketMorningFrontendAuthError";
  }
}

/**
 * Register a deployment-owned session adapter. Tokens stay in the adapter's
 * memory and are requested immediately before each API call; this module never
 * persists them or places them in URLs.
 */
export function setMarketMorningBearerTokenProvider(
  scope: MarketMorningAuthScope,
  provider: MarketMorningBearerTokenProvider,
): void {
  if (typeof provider !== "function") throw new MarketMorningFrontendAuthError();
  providers[scope] = provider;
}

/**
 * Register a deployment-owned OIDC lifecycle wrapper before React renders.
 * The wrapper may use an Auth0 or other approved SDK, but access/refresh tokens
 * remain inside that SDK and are requested only through getAccessToken().
 */
export function setMarketMorningAuthLifecycleAdapter(
  scope: MarketMorningAuthScope,
  adapter: MarketMorningAuthLifecycleAdapter,
): void {
  if (
    !adapter ||
    typeof adapter !== "object" ||
    typeof adapter.initialize !== "function" ||
    typeof adapter.getAccessToken !== "function" ||
    typeof adapter.login !== "function" ||
    typeof adapter.logout !== "function"
  ) {
    throw new MarketMorningFrontendAuthError();
  }
  lifecycleEntries[scope] = {
    adapter,
    initialized: false,
    initialization: null,
  };
}

export function hasMarketMorningAuthLifecycleAdapter(
  scope: MarketMorningAuthScope,
): boolean {
  return lifecycleEntries[scope] !== undefined;
}

export function clearMarketMorningAuthLifecycleAdapters(): void {
  delete lifecycleEntries.product;
  delete lifecycleEntries.operator;
}

export function clearMarketMorningBearerTokenProviders(): void {
  delete providers.product;
  delete providers.operator;
}

export function normalizeMarketMorningReturnTo(value: string): string {
  if (
    typeof value !== "string" ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.length > 2048 ||
    value.includes("\\") ||
    value.includes("#") ||
    CONTROL_CHARACTERS.test(value)
  ) {
    throw new MarketMorningFrontendAuthError();
  }
  try {
    const parsed = new URL(value, "https://market-morning.invalid");
    if (parsed.origin !== "https://market-morning.invalid") {
      throw new MarketMorningFrontendAuthError();
    }
    return `${parsed.pathname}${parsed.search}`;
  } catch (error) {
    if (error instanceof MarketMorningFrontendAuthError) throw error;
    throw new MarketMorningFrontendAuthError();
  }
}

export async function initializeMarketMorningAuth(
  scope: MarketMorningAuthScope,
): Promise<"configured" | "unconfigured"> {
  const entry = lifecycleEntries[scope];
  if (!entry) return "unconfigured";
  if (entry.initialized) return "configured";
  if (!entry.initialization) {
    entry.initialization = Promise.resolve()
      .then(() => entry.adapter.initialize())
      .then(() => {
        entry.initialized = true;
      })
      .catch(() => {
        throw new MarketMorningFrontendAuthError();
      })
      .finally(() => {
        entry.initialization = null;
      });
  }
  await entry.initialization;
  return "configured";
}

export async function beginMarketMorningLogin(
  scope: MarketMorningAuthScope,
  returnTo: string,
): Promise<void> {
  const entry = lifecycleEntries[scope];
  if (!entry) throw new MarketMorningFrontendAuthError();
  const destination = normalizeMarketMorningReturnTo(returnTo);
  await initializeMarketMorningAuth(scope);
  try {
    await entry.adapter.login(destination);
  } catch {
    throw new MarketMorningFrontendAuthError();
  }
}

export async function endMarketMorningSession(
  scope: MarketMorningAuthScope,
  returnTo: string,
): Promise<void> {
  const entry = lifecycleEntries[scope];
  if (!entry) throw new MarketMorningFrontendAuthError();
  const destination = normalizeMarketMorningReturnTo(returnTo);
  await initializeMarketMorningAuth(scope);
  try {
    await entry.adapter.logout(destination);
  } catch {
    throw new MarketMorningFrontendAuthError();
  }
}

function bearerHeader(token: unknown): Record<string, string> {
  if (token === null || token === undefined || token === "") return {};
  if (
    typeof token !== "string" ||
    token.length > MAX_BEARER_LENGTH ||
    token !== token.trim() ||
    CONTROL_CHARACTERS.test(token)
  ) {
    throw new MarketMorningFrontendAuthError();
  }
  return { Authorization: `Bearer ${token}` };
}

export async function marketMorningAuthHeaders(
  scope: MarketMorningAuthScope,
): Promise<Record<string, string>> {
  const lifecycle = lifecycleEntries[scope];
  if (lifecycle) await initializeMarketMorningAuth(scope);
  const provider =
    providers[scope] ??
    (lifecycle ? () => lifecycle.adapter.getAccessToken() : undefined);
  if (!provider) {
    // This fallback is deliberately operator-only. Production preflight blocks
    // deployments without the operator adapter; product identity never reuses
    // the Vibe API key.
    return scope === "operator" ? authHeaders() : {};
  }
  try {
    return bearerHeader(await provider());
  } catch (error) {
    if (error instanceof MarketMorningFrontendAuthError) throw error;
    throw new MarketMorningFrontendAuthError();
  }
}
