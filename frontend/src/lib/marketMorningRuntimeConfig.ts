export type MarketMorningFrontendEnvironment = Record<
  string,
  string | boolean | undefined
>;

export type MarketMorningRuntimeConfigLoadStatus =
  | "ready"
  | "disabled"
  | "misconfigured"
  | "fallback"
  | "unavailable";

interface MarketMorningPublicAuth0Config {
  provider: "auth0";
  domain: string;
  audience: string;
  product_client_id: string;
  operator_client_id: string;
}

interface MarketMorningRuntimeConfigResponse {
  schema_version: 1;
  status: "disabled" | "misconfigured" | "ready";
  feature_enabled: boolean;
  ui_enabled: boolean;
  auth: MarketMorningPublicAuth0Config | null;
  blocking_codes: string[];
}

interface LoadMarketMorningRuntimeConfigOptions {
  allowBuildTimeFallback?: boolean;
  fallbackEnvironment?: MarketMorningFrontendEnvironment;
  fetcher?: typeof fetch;
  timeoutMs?: number;
}

const RUNTIME_CONFIG_PATH = "/market-morning/runtime-config";
const CLOSED_ENVIRONMENT: MarketMorningFrontendEnvironment = {
  VITE_MARKET_MORNING_ENABLED: "false",
  VITE_MARKET_MORNING_AUTH_PROVIDER: "",
};

let runtimeEnvironment: MarketMorningFrontendEnvironment | null = null;

function developmentBuildTimeEnvironment(): MarketMorningFrontendEnvironment {
  if (!import.meta.env.DEV) return {};
  return {
    VITE_MARKET_MORNING_ENABLED: import.meta.env.VITE_MARKET_MORNING_ENABLED,
    VITE_MARKET_MORNING_AUTH_PROVIDER:
      import.meta.env.VITE_MARKET_MORNING_AUTH_PROVIDER,
    VITE_MARKET_MORNING_AUTH0_DOMAIN:
      import.meta.env.VITE_MARKET_MORNING_AUTH0_DOMAIN,
    VITE_MARKET_MORNING_AUTH0_AUDIENCE:
      import.meta.env.VITE_MARKET_MORNING_AUTH0_AUDIENCE,
    VITE_MARKET_MORNING_AUTH0_PRODUCT_CLIENT_ID:
      import.meta.env.VITE_MARKET_MORNING_AUTH0_PRODUCT_CLIENT_ID,
    VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID:
      import.meta.env.VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID,
  };
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim() === value && value.length > 0;
}

function parseResponse(value: unknown): MarketMorningRuntimeConfigResponse | null {
  const payload = objectValue(value);
  if (
    !payload ||
    payload.schema_version !== 1 ||
    !["disabled", "misconfigured", "ready"].includes(String(payload.status)) ||
    typeof payload.feature_enabled !== "boolean" ||
    typeof payload.ui_enabled !== "boolean" ||
    !Array.isArray(payload.blocking_codes) ||
    !payload.blocking_codes.every((code) => nonEmptyString(code))
  ) {
    return null;
  }

  if (payload.status !== "ready") {
    if (payload.ui_enabled || payload.auth !== null) return null;
    return payload as unknown as MarketMorningRuntimeConfigResponse;
  }

  const auth = objectValue(payload.auth);
  if (
    !payload.feature_enabled ||
    !payload.ui_enabled ||
    !auth ||
    auth.provider !== "auth0" ||
    !nonEmptyString(auth.domain) ||
    !nonEmptyString(auth.audience) ||
    !nonEmptyString(auth.product_client_id) ||
    !nonEmptyString(auth.operator_client_id) ||
    payload.blocking_codes.length > 0
  ) {
    return null;
  }
  return payload as unknown as MarketMorningRuntimeConfigResponse;
}

function environmentFromResponse(
  payload: MarketMorningRuntimeConfigResponse,
): MarketMorningFrontendEnvironment {
  if (payload.status !== "ready" || !payload.auth) return { ...CLOSED_ENVIRONMENT };
  return {
    VITE_MARKET_MORNING_ENABLED: "true",
    VITE_MARKET_MORNING_AUTH_PROVIDER: payload.auth.provider,
    VITE_MARKET_MORNING_AUTH0_DOMAIN: payload.auth.domain,
    VITE_MARKET_MORNING_AUTH0_AUDIENCE: payload.auth.audience,
    VITE_MARKET_MORNING_AUTH0_PRODUCT_CLIENT_ID: payload.auth.product_client_id,
    VITE_MARKET_MORNING_AUTH0_OPERATOR_CLIENT_ID: payload.auth.operator_client_id,
  };
}

export function getMarketMorningFrontendEnvironment(
  fallbackEnvironment: MarketMorningFrontendEnvironment =
    developmentBuildTimeEnvironment(),
): MarketMorningFrontendEnvironment {
  return runtimeEnvironment ?? fallbackEnvironment;
}

export function clearMarketMorningRuntimeConfigForTests(): void {
  runtimeEnvironment = null;
}

export async function loadMarketMorningRuntimeConfig(
  options: LoadMarketMorningRuntimeConfigOptions = {},
): Promise<MarketMorningRuntimeConfigLoadStatus> {
  const {
    allowBuildTimeFallback = false,
    fallbackEnvironment = developmentBuildTimeEnvironment(),
    fetcher = fetch,
    timeoutMs = 3_000,
  } = options;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetcher(RUNTIME_CONFIG_PATH, {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error("runtime_config_unavailable");
    const payload = parseResponse(await response.json());
    if (!payload) throw new Error("runtime_config_invalid");
    if (
      allowBuildTimeFallback &&
      payload.status !== "ready" &&
      fallbackEnvironment.VITE_MARKET_MORNING_ENABLED === "true"
    ) {
      runtimeEnvironment = fallbackEnvironment;
      return "fallback";
    }
    runtimeEnvironment = environmentFromResponse(payload);
    return payload.status;
  } catch {
    if (allowBuildTimeFallback) {
      runtimeEnvironment = fallbackEnvironment;
      return "fallback";
    }
    runtimeEnvironment = { ...CLOSED_ENVIRONMENT };
    return "unavailable";
  } finally {
    window.clearTimeout(timeout);
  }
}
