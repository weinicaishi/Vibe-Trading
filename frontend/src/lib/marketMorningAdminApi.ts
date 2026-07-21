import { marketMorningAuthHeaders } from "@/lib/marketMorningAuth";

export class MarketMorningAdminApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "MarketMorningAdminApiError";
    this.status = status;
  }
}

export interface OperationsSourceHealth {
  provider: string;
  status: "healthy" | "error" | "never_run" | string;
  last_successful_discovery_at: string | null;
  last_error_at: string | null;
  last_error_code: string | null;
}

export interface OperationsGlobalRun {
  run_id: string;
  edition_date: string;
  run_version: number;
  attempt_key: string;
  scenario: string;
  status: string;
  is_current: boolean;
  email_permitted: boolean;
  late: boolean;
  reason_code: string | null;
  started_at: string;
  completed_at: string | null;
}

export interface OperationsEventBriefCount {
  status: string;
  review_status: string;
  count: number;
}

export interface OperationsAlert {
  code: string;
  severity: "warning" | "critical";
  count: number;
}

export interface OperationsSummary {
  generated_at: string;
  window_started_at: string;
  cost_observability:
    | "no_invocations"
    | "usage_incomplete"
    | "cost_incomplete"
    | "complete";
  event_brief_generation_attempt_count: number;
  model_usage: {
    invocation_count: number;
    usage_missing_count: number;
    unpriced_count: number;
    input_tokens: number;
    output_tokens: number;
    costs_by_currency: Record<string, number>;
  };
  sources: OperationsSourceHealth[];
  job_counts: Record<string, number>;
  job_failure_counts: Record<string, number>;
  global_runs: OperationsGlobalRun[];
  event_brief_counts: OperationsEventBriefCount[];
  delivery_counts: Record<string, number>;
  engagement_counts: Record<string, number>;
  active_halts: Array<{
    edition_date: string;
    reason_code: string;
    created_at: string;
  }>;
  alerts: OperationsAlert[];
}

export interface DeploymentPreflight {
  status: "blocked" | "configuration_ready";
  scope: "static_configuration_and_database";
  enabled: boolean;
  database_ready: boolean;
  product_auth_configured: boolean;
  admin_auth_configured: boolean;
  oidc_configuration_ready: boolean;
  runtime_enabled: boolean;
  runtime_factory_configured: boolean;
  provider_bundle_factory_configured: boolean;
  email_webhook_configured: boolean;
  email_identity_factory_configured: boolean;
  delivery_link_configuration_ready: boolean;
  synthetic_data_disabled: boolean;
  fixture_runtime_disabled: boolean;
  counts_as_t1_evidence: false;
  blocking_checks: string[];
  timestamp: string;
}

export type EventBriefReviewStatus =
  | "pending"
  | "auto_validated"
  | "approved"
  | "rejected";

export interface EventBriefReviewQueueItem {
  brief_id: string;
  event_id: string;
  event_version: number;
  issuer_id: string;
  issuer_code: string;
  legal_name_ja: string;
  event_title: string;
  status: string;
  review_status: EventBriefReviewStatus;
  model_version: string;
  prompt_version: string;
  attempt_count: number;
  source_count: number;
  validation_errors: string[];
  last_failure_code: string | null;
  completed_at: string | null;
  published_at: string | null;
  updated_at: string;
}

export interface EventBriefReviewQueue {
  items: EventBriefReviewQueueItem[];
  count: number;
}

export interface EventBriefReviewMutation {
  status: "reviewed" | "already_reviewed";
  brief_id: string;
  review_status: "approved" | "rejected";
  reviewed_at: string;
}

export type CatalogReviewStatus = "pending" | "approved" | "rejected";

export interface IssuerAliasReviewQueueItem {
  alias_id: string;
  issuer_id: string;
  issuer_code: string;
  legal_name_ja: string;
  display_alias: string;
  normalized_alias: string;
  source_type: string;
  review_status: CatalogReviewStatus;
  effective_from: string;
  effective_to: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
}

export interface IssuerAliasReviewQueue {
  items: IssuerAliasReviewQueueItem[];
  count: number;
}

export type IssuerAliasReviewReason =
  | "verified_company_name"
  | "ambiguous_alias"
  | "wrong_issuer"
  | "unsupported_source";

export interface IssuerAliasReviewMutation {
  status: "reviewed" | "already_reviewed";
  alias_id: string;
  review_status: "approved" | "rejected";
  reviewed_at: string;
}

export interface EventMergeReviewQueueItem {
  candidate_id: string;
  issuer_id: string;
  issuer_code: string;
  legal_name_ja: string;
  left_event_id: string;
  left_event_title: string;
  left_event_type: string;
  left_occurred_at: string;
  right_event_id: string;
  right_event_title: string;
  right_event_type: string;
  right_occurred_at: string;
  reason: string;
  title_similarity: number;
  time_distance_seconds: number;
  review_status: CatalogReviewStatus;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
}

export interface EventMergeReviewQueue {
  items: EventMergeReviewQueueItem[];
  count: number;
}

export type EventMergeReviewReason =
  | "same_disclosure_event"
  | "distinct_events"
  | "insufficient_evidence";

export interface EventMergeReviewMutation {
  status: "reviewed" | "already_reviewed";
  candidate_id: string;
  review_status: "approved" | "rejected";
  reviewed_at: string;
}

export type ContentReportStatus = "pending" | "resolved" | "dismissed";

export interface ContentReportQueueItem {
  report_id: string;
  edition_date: string;
  event_id: string;
  issuer_id: string;
  issuer_code: string;
  legal_name_ja: string;
  event_title: string;
  event_type: string;
  reason_code: string;
  report_status: ContentReportStatus;
  resolution_code: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ContentReportQueue {
  items: ContentReportQueueItem[];
  count: number;
}

export interface ContentReportReviewMutation {
  status: "reviewed" | "already_reviewed";
  report_id: string;
  report_status: "resolved" | "dismissed";
  resolution_code: string;
  reviewed_at: string;
}

export type PublicationHaltReason =
  | "source_incident"
  | "market_data_incident"
  | "content_quality_incident"
  | "delivery_incident"
  | "operator_review";

export interface PublicationHaltMutation {
  status: "created" | "already_active" | "revoked" | "not_found";
  override: {
    override_id: string;
    edition_date: string;
    reason_code: string;
  } | null;
}

export interface PrivateBetaInviteCreation {
  status: "created";
  invite_id: string;
  raw_token: string;
  expires_at: string;
}

export type UserAccessReason =
  | "beta_access_revoked"
  | "security_incident"
  | "user_request"
  | "support_resolution";

export interface UserAccessMutation {
  status: "suspended" | "already_suspended" | "reactivated" | "already_active";
  user_id: string;
  account_status: "active" | "suspended";
}

async function request<T>(
  path: string,
  options?: RequestInit,
  acceptedErrorStatuses: readonly number[] = [],
): Promise<T> {
  const authorizationHeaders = await marketMorningAuthHeaders("operator");
  const headers = new Headers({
    "Content-Type": "application/json",
    ...authorizationHeaders,
  });
  if (options?.headers) {
    new Headers(options.headers).forEach((value, key) => headers.set(key, value));
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok && !acceptedErrorStatuses.includes(response.status)) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Keep the status-only error; do not include an arbitrary response body.
    }
    throw new MarketMorningAdminApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export const marketMorningAdminApi = {
  getDeploymentPreflight: () =>
    request<DeploymentPreflight>(
      "/market-morning/_internal/deployment-preflight",
      undefined,
      [503],
    ),
  getSummary: (hours = 24, recentRunLimit = 10) =>
    request<OperationsSummary>(
      `/market-morning/_internal/operations/summary?hours=${encodeURIComponent(String(hours))}&recent_run_limit=${encodeURIComponent(String(recentRunLimit))}`,
    ),
  listEventBriefs: (
    reviewStatus: EventBriefReviewStatus | null = "auto_validated",
    limit = 20,
  ) => {
    const query = new URLSearchParams();
    if (reviewStatus) query.set("review_status", reviewStatus);
    query.set("limit", String(limit));
    return request<EventBriefReviewQueue>(
      `/market-morning/_internal/event-briefs?${query.toString()}`,
    );
  },
  reviewEventBrief: (
    briefId: string,
    decision: "approve" | "reject",
    reasonCode: string,
  ) =>
    request<EventBriefReviewMutation>(
      `/market-morning/_internal/event-briefs/${encodeURIComponent(briefId)}/review`,
      {
        method: "POST",
        body: JSON.stringify({ decision, reason_code: reasonCode }),
      },
    ),
  listIssuerAliases: (
    reviewStatus: CatalogReviewStatus | null = "pending",
    limit = 50,
  ) => {
    const query = new URLSearchParams();
    if (reviewStatus) query.set("review_status", reviewStatus);
    query.set("limit", String(limit));
    return request<IssuerAliasReviewQueue>(
      `/market-morning/_internal/issuer-aliases?${query.toString()}`,
    );
  },
  reviewIssuerAlias: (
    aliasId: string,
    decision: "approve" | "reject",
    reasonCode: IssuerAliasReviewReason,
  ) =>
    request<IssuerAliasReviewMutation>(
      `/market-morning/_internal/issuer-aliases/${encodeURIComponent(aliasId)}/review`,
      {
        method: "POST",
        body: JSON.stringify({ decision, reason_code: reasonCode }),
      },
    ),
  listEventMergeCandidates: (
    reviewStatus: CatalogReviewStatus | null = "pending",
    limit = 50,
  ) => {
    const query = new URLSearchParams();
    if (reviewStatus) query.set("review_status", reviewStatus);
    query.set("limit", String(limit));
    return request<EventMergeReviewQueue>(
      `/market-morning/_internal/event-merge-candidates?${query.toString()}`,
    );
  },
  reviewEventMergeCandidate: (
    candidateId: string,
    decision: "approve" | "reject",
    reasonCode: EventMergeReviewReason,
  ) =>
    request<EventMergeReviewMutation>(
      `/market-morning/_internal/event-merge-candidates/${encodeURIComponent(candidateId)}/review`,
      {
        method: "POST",
        body: JSON.stringify({ decision, reason_code: reasonCode }),
      },
    ),
  listContentReports: (
    reportStatus: ContentReportStatus | null = "pending",
    limit = 50,
  ) => {
    const query = new URLSearchParams();
    if (reportStatus) query.set("report_status", reportStatus);
    query.set("limit", String(limit));
    return request<ContentReportQueue>(
      `/market-morning/_internal/content-reports?${query.toString()}`,
    );
  },
  reviewContentReport: (
    reportId: string,
    decision: "resolve" | "dismiss",
    resolutionCode:
      | "brief_rejected"
      | "source_corrected"
      | "content_revised"
      | "duplicate_report"
      | "no_issue_found"
      | "insufficient_evidence",
  ) =>
    request<ContentReportReviewMutation>(
      `/market-morning/_internal/content-reports/${encodeURIComponent(reportId)}/review`,
      {
        method: "POST",
        body: JSON.stringify({
          decision,
          resolution_code: resolutionCode,
        }),
      },
    ),
  createPublicationHalt: (
    editionDate: string,
    reasonCode: PublicationHaltReason,
  ) =>
    request<PublicationHaltMutation>(
      "/market-morning/_internal/operations/halts",
      {
        method: "POST",
        body: JSON.stringify({
          edition_date: editionDate,
          reason_code: reasonCode,
        }),
      },
    ),
  revokePublicationHalt: (editionDate: string) =>
    request<PublicationHaltMutation>(
      `/market-morning/_internal/operations/halts/${encodeURIComponent(editionDate)}`,
      { method: "DELETE" },
    ),
  createPrivateBetaInvite: (expiresInDays: number) =>
    request<PrivateBetaInviteCreation>(
      "/market-morning/_internal/private-beta/invites",
      {
        method: "POST",
        body: JSON.stringify({ expires_in_days: expiresInDays }),
      },
    ),
  revokePrivateBetaInvite: (inviteId: string) =>
    request(
      `/market-morning/_internal/private-beta/invites/${encodeURIComponent(inviteId)}`,
      { method: "DELETE" },
    ),
  setUserAccess: (
    userId: string,
    action: "suspend" | "reactivate",
    reasonCode: UserAccessReason,
  ) =>
    request<UserAccessMutation>(
      `/market-morning/_internal/users/${encodeURIComponent(userId)}/access`,
      {
        method: "POST",
        body: JSON.stringify({ action, reason_code: reasonCode }),
      },
    ),
};
