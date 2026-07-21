import { marketMorningAuthHeaders } from "@/lib/marketMorningAuth";

export class MarketMorningApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "MarketMorningApiError";
    this.status = status;
  }
}

export type MarketMorningAccessState =
  | "disabled"
  | "authentication_unconfigured"
  | "unauthenticated"
  | "unavailable"
  | "unknown";

export interface ConsentState {
  consent_type: "risk_disclosure" | "data_disclosure";
  accepted: boolean;
  consent_version: string | null;
  accepted_at: string | null;
  revoked_at: string | null;
}

export interface MarketMorningSettings {
  timezone: string;
  email_opt_in: boolean;
  risk_disclosure: ConsentState;
  data_disclosure: ConsentState;
}

export interface WatchlistItem {
  watchlist_item_id: string;
  issuer_id: string;
  issuer_code: string;
  legal_name_ja: string;
  market_segment: string;
  issuer_status: string;
  user_label: string | null;
  sort_order: number;
  created_at: string;
}

export interface WatchlistResponse {
  items: WatchlistItem[];
  active_count: number;
  limit: number;
}

export interface WatchlistMutationResponse {
  status: "added" | "already_active" | "updated" | "removed" | "already_removed";
  item: WatchlistItem | null;
  active_count: number;
  limit: number;
}

export interface IssuerSearchItem {
  issuer_id: string;
  issuer_code: string;
  legal_name_ja: string;
  market_segment: string;
  match_kind:
    | "code_exact"
    | "official_exact"
    | "alias_exact"
    | "official_prefix"
    | "alias_prefix";
  matched_alias: string | null;
}

export interface IssuerSearchResponse {
  query: string;
  normalized_query: string;
  items: IssuerSearchItem[];
  no_match_guidance: string | null;
}

export interface AccountDeletionRequestResponse {
  status: "requested" | "already_requested";
  request_id: string;
  request_status: "pending" | "processing";
  requested_at: string;
}

export interface AccountDataExport {
  schema_version: 1;
  generated_at: string;
  data: Record<string, Array<Record<string, unknown>>>;
}

export interface DeliveryLinkRedeemResponse {
  status: "accepted";
  edition_date: string;
  destination_path: "/market-morning";
}

export type MorningEditionStatus =
  | "ready"
  | "partial"
  | "no_data"
  | "market_holiday";

export interface EditionSourceCitation {
  provider: string;
  document_id: string;
  revision_key: string;
  original_url: string;
  published_at: string;
}

export interface EditionFact {
  kind: "fact";
  text: string;
  event_id: string;
  event_family_key: string;
  event_version: number;
  event_type: string;
  occurred_at: string;
  lifecycle_status: "active" | "corrected" | "withdrawn";
  citations: EditionSourceCitation[];
}

export interface EditionAssessment {
  kind: "inference";
  text: string;
  evidence_quality: "insufficient_evidence";
}

export interface EditionIssuer {
  issuer_id: string;
  issuer_code: string;
  legal_name_ja: string;
  status:
    | "ready"
    | "partial"
    | "no_confirmed_events"
    | "insufficient_data"
    | "unavailable";
  facts: EditionFact[];
  assessment: EditionAssessment;
  omitted_event_count: number;
  warnings: string[];
  error_code: string | null;
}

export interface EditionDayPlan {
  edition_date: string;
  generate: boolean;
  status: "scheduled" | "market_holiday";
  overnight_context: "us_session_available" | "us_market_closed";
  reason_code: string | null;
  us_reason_code: string | null;
}

export interface EditionBudget {
  max_issuers: number;
  max_events_per_issuer: number;
  max_total_events: number;
}

export interface MorningEdition {
  edition_date: string;
  generated_at: string;
  status: MorningEditionStatus;
  day_plan: EditionDayPlan;
  issuers: EditionIssuer[];
  consumed_event_units: number;
  budget: EditionBudget;
  budget_exhausted: boolean;
  omitted_issuer_count: number;
}

export interface TodayEditionResponse {
  status: MorningEditionStatus | "not_published";
  data_mode: "synthetic_fixture" | "persisted" | "unavailable";
  edition_id: string | null;
  reason_code: string | null;
  fixture_version: string | null;
  edition: MorningEdition | null;
}

export type EditionEventState = "read" | "later" | "irrelevant";

export interface EditionEventStateItem {
  edition_id: string;
  event_id: string;
  state: EditionEventState;
  first_read_at: string | null;
  updated_at: string;
}

export interface EditionEventStatesResponse {
  items: EditionEventStateItem[];
}

export interface EditionSourceOpenPayload {
  event_id: string;
  provider: string;
  document_id: string;
  revision_key: string;
  request_id: string;
}

export interface EditionSourceOpenResponse {
  status: "recorded" | "already_recorded";
  source_open_id: string;
  original_url: string;
}

export type ContentReportReason =
  | "fact_inaccurate"
  | "source_mismatch"
  | "outdated_or_corrected"
  | "other_content_issue";

export interface ContentReportCreateResponse {
  status: "reported" | "already_reported";
  report_id: string;
  reason_code: ContentReportReason;
  report_status: "pending" | "resolved" | "dismissed";
  created_at: string;
}

export interface IssuerResearchNote {
  note_id: string;
  text: string;
  created_at: string;
  updated_at: string;
}

export interface IssuerResearchFact {
  text: string;
  citations: EditionSourceCitation[];
}

export interface IssuerResearchEvent {
  event_id: string;
  event_family_key: string;
  event_version: number;
  title: string;
  event_type: string;
  occurred_at: string;
  lifecycle_status: "active" | "corrected" | "withdrawn";
  supersedes_event_id: string | null;
  facts: IssuerResearchFact[];
  citations: EditionSourceCitation[];
  warnings: string[];
}

export interface IssuerResearchResponse {
  issuer_id: string;
  issuer_code: string;
  legal_name_ja: string;
  market_segment: string;
  note: IssuerResearchNote | null;
  events: IssuerResearchEvent[];
}

async function marketMorningError(res: Response): Promise<MarketMorningApiError> {
  let detail = `HTTP ${res.status}`;
  try {
    const body = (await res.json()) as { detail?: unknown; message?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
    else if (typeof body.message === "string") detail = body.message;
  } catch {
    // Keep the status-only fallback. Never include an HTML response body.
  }
  return new MarketMorningApiError(detail, res.status);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const { headers, ...rest } = options ?? {};
  const mergedHeaders = new Headers(headers);
  const authorizationHeaders = await marketMorningAuthHeaders("product");
  Object.entries(authorizationHeaders).forEach(([key, value]) => {
    mergedHeaders.set(key, value);
  });
  mergedHeaders.set("Accept", "application/json");
  if (rest.body !== undefined) mergedHeaders.set("Content-Type", "application/json");

  const response = await fetch(path, {
    ...rest,
    headers: mergedHeaders,
    credentials: "same-origin",
  });
  if (!response.ok) throw await marketMorningError(response);

  const text = await response.text();
  if (!text) return {} as T;
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    throw new MarketMorningApiError(
      `Expected JSON from ${path}, received ${contentType || "unknown content type"}`,
      response.status,
    );
  }
  return JSON.parse(text) as T;
}

export function classifyMarketMorningAccessError(error: unknown): MarketMorningAccessState {
  if (!(error instanceof MarketMorningApiError)) return "unknown";
  const message = error.message.toLowerCase();
  if (message.includes("market morning is disabled")) return "disabled";
  if (message.includes("product authentication is not configured")) {
    return "authentication_unconfigured";
  }
  if (error.status === 401 || error.status === 403) return "unauthenticated";
  if (error.status === 503) return "unavailable";
  return "unknown";
}

export const marketMorningApi = {
  getSettings: (signal?: AbortSignal) =>
    request<MarketMorningSettings>("/market-morning/settings", { signal }),
  updateSettings: (payload: { timezone?: string; email_opt_in?: boolean }) =>
    request<MarketMorningSettings>("/market-morning/settings", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  exportAccountData: () =>
    request<AccountDataExport>("/market-morning/account-data-export"),
  getWatchlist: (signal?: AbortSignal) =>
    request<WatchlistResponse>("/market-morning/watchlist", { signal }),
  getTodayEdition: (signal?: AbortSignal) =>
    request<TodayEditionResponse>("/market-morning/edition/today", { signal }),
  redeemDeliveryLink: (token: string) =>
    request<DeliveryLinkRedeemResponse>("/market-morning/delivery-links/redeem", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  getEditionEventStates: (editionId: string, signal?: AbortSignal) =>
    request<EditionEventStatesResponse>(
      `/market-morning/editions/${encodeURIComponent(editionId)}/event-states`,
      { signal },
    ),
  updateEditionEventState: (
    editionId: string,
    eventId: string,
    state: EditionEventState,
  ) =>
    request<EditionEventStateItem>(
      `/market-morning/editions/${encodeURIComponent(editionId)}/events/${encodeURIComponent(eventId)}/state`,
      { method: "PATCH", body: JSON.stringify({ state }) },
    ),
  recordEditionSourceOpen: (
    editionId: string,
    payload: EditionSourceOpenPayload,
  ) =>
    request<EditionSourceOpenResponse>(
      `/market-morning/editions/${encodeURIComponent(editionId)}/sources/open`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  reportEditionEvent: (
    editionId: string,
    eventId: string,
    reasonCode: ContentReportReason,
  ) =>
    request<ContentReportCreateResponse>(
      `/market-morning/editions/${encodeURIComponent(editionId)}/events/${encodeURIComponent(eventId)}/report`,
      { method: "POST", body: JSON.stringify({ reason_code: reasonCode }) },
    ),
  getIssuerResearch: (issuerId: string, signal?: AbortSignal) =>
    request<IssuerResearchResponse>(
      `/market-morning/issuers/${encodeURIComponent(issuerId)}/research?limit=50`,
      { signal },
    ),
  updateIssuerNote: (issuerId: string, text: string) =>
    request<{ status: "saved"; note: IssuerResearchNote }>(
      `/market-morning/issuers/${encodeURIComponent(issuerId)}/note`,
      { method: "PUT", body: JSON.stringify({ text }) },
    ),
  deleteIssuerNote: (issuerId: string) =>
    request<{ status: "cleared" | "already_clear" }>(
      `/market-morning/issuers/${encodeURIComponent(issuerId)}/note`,
      { method: "DELETE" },
    ),
  searchIssuers: (query: string, signal?: AbortSignal) =>
    request<IssuerSearchResponse>(
      `/market-morning/issuers/search?q=${encodeURIComponent(query)}&limit=10`,
      { signal },
    ),
  addWatchlistItem: (issuerId: string) =>
    request<WatchlistMutationResponse>("/market-morning/watchlist", {
      method: "POST",
      body: JSON.stringify({ issuer_id: issuerId }),
    }),
  removeWatchlistItem: (issuerId: string) =>
    request<WatchlistMutationResponse>(
      `/market-morning/watchlist/${encodeURIComponent(issuerId)}`,
      { method: "DELETE" },
    ),
  recordConsent: (payload: {
    consent_type: "risk_disclosure" | "data_disclosure";
    consent_version: string;
    accepted: boolean;
  }) =>
    request<{ status: string; consent: ConsentState }>("/market-morning/consents", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  requestAccountDeletion: () =>
    request<AccountDeletionRequestResponse>("/market-morning/account-deletion-requests", {
      method: "POST",
    }),
};
