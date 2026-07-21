import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const mocks = vi.hoisted(() => ({
  getDeploymentPreflight: vi.fn(),
  getSummary: vi.fn(),
  listEventBriefs: vi.fn(),
  reviewEventBrief: vi.fn(),
  listContentReports: vi.fn(),
  reviewContentReport: vi.fn(),
  listIssuerAliases: vi.fn(),
  reviewIssuerAlias: vi.fn(),
  listEventMergeCandidates: vi.fn(),
  reviewEventMergeCandidate: vi.fn(),
  createPublicationHalt: vi.fn(),
  revokePublicationHalt: vi.fn(),
  createPrivateBetaInvite: vi.fn(),
  revokePrivateBetaInvite: vi.fn(),
  setUserAccess: vi.fn(),
}));

vi.mock("@/lib/marketMorningAdminApi", () => ({
  marketMorningAdminApi: {
    getDeploymentPreflight: mocks.getDeploymentPreflight,
    getSummary: mocks.getSummary,
    listEventBriefs: mocks.listEventBriefs,
    reviewEventBrief: mocks.reviewEventBrief,
    listContentReports: mocks.listContentReports,
    reviewContentReport: mocks.reviewContentReport,
    listIssuerAliases: mocks.listIssuerAliases,
    reviewIssuerAlias: mocks.reviewIssuerAlias,
    listEventMergeCandidates: mocks.listEventMergeCandidates,
    reviewEventMergeCandidate: mocks.reviewEventMergeCandidate,
    createPublicationHalt: mocks.createPublicationHalt,
    revokePublicationHalt: mocks.revokePublicationHalt,
    createPrivateBetaInvite: mocks.createPrivateBetaInvite,
    revokePrivateBetaInvite: mocks.revokePrivateBetaInvite,
    setUserAccess: mocks.setUserAccess,
  },
}));

import { MarketMorningOperations } from "../MarketMorningOperations";
import {
  clearMarketMorningAuthLifecycleAdapters,
  clearMarketMorningBearerTokenProviders,
  setMarketMorningAuthLifecycleAdapter,
} from "@/lib/marketMorningAuth";

const SUMMARY = {
  generated_at: "2026-07-21T00:30:00Z",
  window_started_at: "2026-07-20T00:30:00Z",
  cost_observability: "usage_incomplete" as const,
  event_brief_generation_attempt_count: 11,
  model_usage: {
    invocation_count: 11,
    usage_missing_count: 1,
    unpriced_count: 2,
    input_tokens: 1000,
    output_tokens: 250,
    costs_by_currency: { USD: 1234 },
  },
  sources: [
    {
      provider: "tdnet",
      status: "healthy",
      last_successful_discovery_at: "2026-07-21T00:20:00Z",
      last_error_at: null,
      last_error_code: null,
    },
    {
      provider: "edinet",
      status: "error",
      last_successful_discovery_at: null,
      last_error_at: "2026-07-21T00:25:00Z",
      last_error_code: "source_timeout",
    },
  ],
  job_counts: { succeeded: 12, failed: 2 },
  job_failure_counts: { source_timeout: 2 },
  global_runs: [
    {
      run_id: "run-1",
      edition_date: "2026-07-21",
      run_version: 1,
      attempt_key: "0700",
      scenario: "standard",
      status: "complete",
      is_current: true,
      email_permitted: true,
      late: false,
      reason_code: null,
      started_at: "2026-07-21T00:00:00Z",
      completed_at: "2026-07-21T00:05:00Z",
    },
  ],
  event_brief_counts: [
    { status: "published", review_status: "auto_validated", count: 8 },
    { status: "blocked", review_status: "pending", count: 1 },
  ],
  delivery_counts: { sent: 5, failed: 1 },
  engagement_counts: { edition_opened: 4, source_opened: 7 },
  active_halts: [],
  alerts: [
    { code: "source_error", severity: "critical" as const, count: 1 },
    { code: "model_usage_missing", severity: "warning" as const, count: 1 },
    { code: "model_cost_missing", severity: "warning" as const, count: 2 },
  ],
};

const QUEUE = {
  count: 1,
  items: [
    {
      brief_id: "brief-1",
      event_id: "event-1",
      event_version: 1,
      issuer_id: "issuer-1",
      issuer_code: "7203",
      legal_name_ja: "トヨタ自動車株式会社",
      event_title: "決算短信",
      status: "published",
      review_status: "auto_validated",
      model_version: "model-v1",
      prompt_version: "prompt-v1",
      attempt_count: 1,
      source_count: 2,
      validation_errors: [],
      last_failure_code: null,
      completed_at: "2026-07-21T00:10:00Z",
      published_at: "2026-07-21T00:10:00Z",
      updated_at: "2026-07-21T00:10:00Z",
    },
  ],
};

const PREFLIGHT = {
  status: "blocked" as const,
  scope: "static_configuration_and_database" as const,
  enabled: true,
  database_ready: false,
  product_auth_configured: true,
  admin_auth_configured: true,
  oidc_configuration_ready: true,
  runtime_enabled: true,
  runtime_factory_configured: true,
  provider_bundle_factory_configured: false,
  email_webhook_configured: true,
  email_identity_factory_configured: true,
  delivery_link_configuration_ready: true,
  synthetic_data_disabled: true,
  fixture_runtime_disabled: true,
  counts_as_t1_evidence: false as const,
  blocking_checks: ["database_not_ready"],
  timestamp: "2026-07-21T01:00:00Z",
};

const CONTENT_REPORTS = {
  count: 1,
  items: [
    {
      report_id: "88888888-8888-4888-8888-888888888888",
      edition_date: "2026-07-21",
      event_id: "33333333-3333-4333-8333-333333333333",
      issuer_id: "22222222-2222-4222-8222-222222222222",
      issuer_code: "7203",
      legal_name_ja: "トヨタ自動車株式会社",
      event_title: "業績予想の修正",
      event_type: "guidance_revision",
      reason_code: "source_mismatch",
      report_status: "pending",
      resolution_code: null,
      reviewed_by: null,
      reviewed_at: null,
      created_at: "2026-07-21T01:00:00Z",
      updated_at: "2026-07-21T01:00:00Z",
    },
  ],
};

const ISSUER_ALIASES = {
  count: 1,
  items: [
    {
      alias_id: "11111111-1111-4111-8111-111111111111",
      issuer_id: "22222222-2222-4222-8222-222222222222",
      issuer_code: "7203",
      legal_name_ja: "トヨタ自動車株式会社",
      display_alias: "トヨタ",
      normalized_alias: "トヨタ",
      source_type: "operator",
      review_status: "pending",
      effective_from: "2026-07-01",
      effective_to: null,
      reviewed_by: null,
      reviewed_at: null,
      created_at: "2026-07-21T01:00:00Z",
    },
  ],
};

const EVENT_MERGE_CANDIDATES = {
  count: 1,
  items: [
    {
      candidate_id: "33333333-3333-4333-8333-333333333333",
      issuer_id: "22222222-2222-4222-8222-222222222222",
      issuer_code: "7203",
      legal_name_ja: "トヨタ自動車株式会社",
      left_event_id: "44444444-4444-4444-8444-444444444444",
      left_event_title: "通期業績予想の修正",
      left_event_type: "guidance_revision",
      left_occurred_at: "2026-07-21T00:15:00Z",
      right_event_id: "55555555-5555-4555-8555-555555555555",
      right_event_title: "業績予想修正のお知らせ",
      right_event_type: "guidance_revision",
      right_occurred_at: "2026-07-21T00:17:00Z",
      reason: "same_issuer_similar_title_24h",
      title_similarity: 0.91,
      time_distance_seconds: 120,
      review_status: "pending",
      reviewed_by: null,
      reviewed_at: null,
      created_at: "2026-07-21T01:00:00Z",
    },
  ],
};

describe("MarketMorningOperations", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearMarketMorningAuthLifecycleAdapters();
    clearMarketMorningBearerTokenProviders();
    mocks.getDeploymentPreflight.mockResolvedValue(PREFLIGHT);
    mocks.getSummary.mockResolvedValue(SUMMARY);
    mocks.listEventBriefs.mockResolvedValue(QUEUE);
    mocks.reviewEventBrief.mockResolvedValue({
      status: "reviewed",
      brief_id: "brief-1",
      review_status: "approved",
      reviewed_at: "2026-07-21T00:31:00Z",
    });
    mocks.listContentReports.mockResolvedValue(CONTENT_REPORTS);
    mocks.reviewContentReport.mockResolvedValue({
      status: "reviewed",
      report_id: "88888888-8888-4888-8888-888888888888",
      report_status: "resolved",
      resolution_code: "content_revised",
      reviewed_at: "2026-07-21T01:10:00Z",
    });
    mocks.listIssuerAliases.mockResolvedValue(ISSUER_ALIASES);
    mocks.reviewIssuerAlias.mockResolvedValue({
      status: "reviewed",
      alias_id: "11111111-1111-4111-8111-111111111111",
      review_status: "approved",
      reviewed_at: "2026-07-21T01:10:00Z",
    });
    mocks.listEventMergeCandidates.mockResolvedValue(EVENT_MERGE_CANDIDATES);
    mocks.reviewEventMergeCandidate.mockResolvedValue({
      status: "reviewed",
      candidate_id: "33333333-3333-4333-8333-333333333333",
      review_status: "rejected",
      reviewed_at: "2026-07-21T01:10:00Z",
    });
    mocks.createPublicationHalt.mockResolvedValue({ status: "created", override: null });
    mocks.revokePublicationHalt.mockResolvedValue({ status: "revoked", override: null });
    mocks.createPrivateBetaInvite.mockResolvedValue({
      status: "created",
      invite_id: "invite-1",
      raw_token: "secret-once",
      expires_at: "2026-07-28T01:00:00Z",
    });
    mocks.revokePrivateBetaInvite.mockResolvedValue({ status: "revoked" });
    mocks.setUserAccess.mockResolvedValue({
      status: "suspended",
      user_id: "11111111-1111-4111-8111-111111111111",
      account_status: "suspended",
    });
  });

  afterEach(() => {
    clearMarketMorningAuthLifecycleAdapters();
    clearMarketMorningBearerTokenProviders();
  });

  it("initializes the operator lifecycle before loading private operations APIs", async () => {
    const order: string[] = [];
    setMarketMorningAuthLifecycleAdapter("operator", {
      initialize: vi.fn(async () => {
        order.push("initialize");
      }),
      getAccessToken: () => "operator-token",
      login: vi.fn(),
      logout: vi.fn(),
    });
    mocks.getDeploymentPreflight.mockImplementation(async () => {
      order.push("api");
      return PREFLIGHT;
    });

    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Market Morning 運用" })).toBeInTheDocument();
    await waitFor(() => expect(mocks.getSummary).toHaveBeenCalledOnce());
    expect(order[0]).toBe("initialize");
    expect(order).toContain("api");
  });

  it("offers operator login after a 401 and reloads data after an in-place login", async () => {
    const login = vi.fn();
    setMarketMorningAuthLifecycleAdapter("operator", {
      initialize: vi.fn(),
      getAccessToken: () => null,
      login,
      logout: vi.fn(),
    });
    mocks.getSummary
      .mockRejectedValueOnce(Object.assign(new Error("HTTP 401"), { status: 401 }))
      .mockResolvedValue(SUMMARY);

    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "運用アカウントでログイン" }),
    );
    await waitFor(() => {
      expect(login).toHaveBeenCalledWith("/market-morning-ops");
      expect(mocks.getSummary).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("デプロイ準備")).toBeInTheDocument();
  });

  it("exposes operator logout only when a lifecycle adapter is configured", async () => {
    const logout = vi.fn();
    setMarketMorningAuthLifecycleAdapter("operator", {
      initialize: vi.fn(),
      getAccessToken: () => "operator-token",
      login: vi.fn(),
      logout,
    });

    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "ログアウト" }));
    await waitFor(() => expect(logout).toHaveBeenCalledWith("/"));
    expect(await screen.findByText("運用ログインが必要です")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Market Morning 運用" })).not.toBeInTheDocument();
  });

  it("fails closed on operator initialization errors and retries before API access", async () => {
    const initialize = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error("identity unavailable"))
      .mockResolvedValue(undefined);
    setMarketMorningAuthLifecycleAdapter("operator", {
      initialize,
      getAccessToken: () => "operator-token",
      login: vi.fn(),
      logout: vi.fn(),
    });

    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    expect(await screen.findByText("運用ログイン状態を初期化できませんでした。")).toBeInTheDocument();
    expect(mocks.getSummary).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "認証を再試行" }));

    await waitFor(() => expect(initialize).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(mocks.getSummary).toHaveBeenCalledOnce());
  });

  it("shows operational alerts, source health, run state and measured model cost", async () => {
    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Market Morning 運用" })).toBeInTheDocument();
    expect(screen.getByText("source_timeout")).toBeInTheDocument();
    expect(screen.getByText("Usage 欠損: 1")).toBeInTheDocument();
    expect(screen.getByText("未価格: 2")).toBeInTheDocument();
    expect(screen.getByText("入力 1,000 / 出力 250 tokens")).toBeInTheDocument();
    expect(screen.getByText("USD 0.001234")).toBeInTheDocument();
    expect(screen.getByText("2026-07-21 / v1")).toBeInTheDocument();
    expect(screen.getAllByText("トヨタ自動車株式会社").length).toBeGreaterThan(0);
    expect(screen.getByText("デプロイ準備")).toBeInTheDocument();
    expect(screen.getByText("database_not_ready")).toBeInTheDocument();
    expect(screen.getByText(/T1 の証拠にはなりません/)).toBeInTheDocument();
  });

  it("shows the missing personal operator identity as a deployment blocker", async () => {
    mocks.getDeploymentPreflight.mockResolvedValue({
      ...PREFLIGHT,
      admin_auth_configured: false,
      blocking_checks: ["admin_auth_factory_missing"],
    });

    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    expect(await screen.findByText("admin_auth_factory_missing")).toBeInTheDocument();
    expect(screen.getByText(/個人を識別できる運用認証/)).toBeInTheDocument();
  });

  it("shows email identity and private-link configuration blockers", async () => {
    mocks.getDeploymentPreflight.mockResolvedValue({
      ...PREFLIGHT,
      email_identity_factory_configured: false,
      delivery_link_configuration_ready: false,
      blocking_checks: [
        "email_identity_factory_missing",
        "delivery_link_configuration_invalid",
      ],
    });

    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    expect(await screen.findByText("email_identity_factory_missing")).toBeInTheDocument();
    expect(screen.getByText(/検証済みメール identity factory/)).toBeInTheDocument();
    expect(screen.getByText("delivery_link_configuration_invalid")).toBeInTheDocument();
    expect(screen.getByText(/私有メールリンクの URL または署名鍵/)).toBeInTheDocument();
  });

  it("approves an EventBrief and refreshes the queue", async () => {
    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );
    const approve = await screen.findByRole("button", { name: "承認" });
    fireEvent.click(approve);

    await waitFor(() => {
      expect(mocks.reviewEventBrief).toHaveBeenCalledWith(
        "brief-1",
        "approve",
        "manual_quality_review",
      );
    });
    await waitFor(() => expect(mocks.listEventBriefs).toHaveBeenCalledTimes(2));
  });

  it("shows and resolves a privacy-safe user content report", async () => {
    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    expect(await screen.findByText("ユーザー報告")).toBeInTheDocument();
    expect(screen.getByText("業績予想の修正")).toBeInTheDocument();
    expect(screen.getByText("出典と内容が一致しない")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "内容を修正済み" }));

    await waitFor(() => {
      expect(mocks.reviewContentReport).toHaveBeenCalledWith(
        "88888888-8888-4888-8888-888888888888",
        "resolve",
        "content_revised",
      );
    });
    await waitFor(() => expect(mocks.listContentReports).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/user_id/i)).not.toBeInTheDocument();
  });

  it("reviews issuer aliases and merge candidates without auto-merging events", async () => {
    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    expect(await screen.findByText("銘柄別名レビュー")).toBeInTheDocument();
    expect(screen.getByText("トヨタ")).toBeInTheDocument();
    expect(screen.getByText("跨ソース重複候補")).toBeInTheDocument();
    expect(screen.getByText("通期業績予想の修正")).toBeInTheDocument();
    expect(screen.getByText("業績予想修正のお知らせ")).toBeInTheDocument();
    expect(screen.getByText(/承認してもイベントは自動統合されません/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "別名を承認" }));
    await waitFor(() => {
      expect(mocks.reviewIssuerAlias).toHaveBeenCalledWith(
        "11111111-1111-4111-8111-111111111111",
        "approve",
        "verified_company_name",
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "別イベント" }));
    await waitFor(() => {
      expect(mocks.reviewEventMergeCandidate).toHaveBeenCalledWith(
        "33333333-3333-4333-8333-333333333333",
        "reject",
        "distinct_events",
      );
    });
    expect(screen.queryByText(/source_url/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/payload/i)).not.toBeInTheDocument();
  });

  it("creates a publication halt without offering a force-publish action", async () => {
    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    const dateInput = await screen.findByLabelText("停発対象日");
    fireEvent.change(dateInput, { target: { value: "2026-07-22" } });
    fireEvent.click(screen.getByRole("button", { name: "停発する" }));

    await waitFor(() => {
      expect(mocks.createPublicationHalt).toHaveBeenCalledWith(
        "2026-07-22",
        "content_quality_incident",
      );
    });
    expect(screen.queryByRole("button", { name: /強制公開/ })).not.toBeInTheDocument();
  });

  it("creates a one-time beta invite and suspends a selected user", async () => {
    render(
      <MemoryRouter>
        <MarketMorningOperations />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "招待を発行" }));
    expect(await screen.findByText("secret-once")).toBeInTheDocument();
    expect(screen.getByText(/このトークンは再表示できません/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("ユーザーID"), {
      target: { value: "11111111-1111-4111-8111-111111111111" },
    });
    fireEvent.click(screen.getByRole("button", { name: "利用を停止" }));

    await waitFor(() => {
      expect(mocks.setUserAccess).toHaveBeenCalledWith(
        "11111111-1111-4111-8111-111111111111",
        "suspend",
        "beta_access_revoked",
      );
    });
  });
});
