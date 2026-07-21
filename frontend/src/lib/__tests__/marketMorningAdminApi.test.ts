import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

async function loadApi() {
  vi.resetModules();
  return import("../marketMorningAdminApi");
}

describe("Market Morning admin API", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", {
      getItem: vi.fn(() => "internal-api-key"),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("loads the privacy-safe operations summary with internal auth", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ generated_at: "2026-07-21T00:30:00Z", alerts: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { marketMorningAdminApi } = await loadApi();

    await marketMorningAdminApi.getSummary(24, 10);

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/market-morning/_internal/operations/summary?hours=24&recent_run_limit=10",
    );
    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("Authorization")).toBe("Bearer internal-api-key");
  });

  it("prefers the deployment operator token provider over the legacy API key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ generated_at: "2026-07-21T00:30:00Z", alerts: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { setMarketMorningBearerTokenProvider } = await import(
      "../marketMorningAuth"
    );
    setMarketMorningBearerTokenProvider("operator", () => "oidc-operator-token");
    const { marketMorningAdminApi } = await import("../marketMorningAdminApi");

    await marketMorningAdminApi.getSummary(24, 10);

    const options = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(options.headers).get("Authorization")).toBe(
      "Bearer oidc-operator-token",
    );
    expect(options.credentials).toBe("same-origin");
  });

  it("reads a blocked deployment preflight without treating 503 as a transport failure", async () => {
    const payload = {
      status: "blocked",
      scope: "static_configuration_and_database",
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
      counts_as_t1_evidence: false,
      blocking_checks: ["database_not_ready"],
      timestamp: "2026-07-21T01:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 503,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { marketMorningAdminApi } = await loadApi();

    await expect(marketMorningAdminApi.getDeploymentPreflight()).resolves.toEqual(payload);

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/market-morning/_internal/deployment-preflight",
    );
    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("Authorization")).toBe("Bearer internal-api-key");
  });

  it("lists and reviews EventBriefs without sending model payloads", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], count: 0 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "reviewed",
            brief_id: "brief-1",
            review_status: "rejected",
            reviewed_at: "2026-07-21T00:30:00Z",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const { marketMorningAdminApi } = await loadApi();

    await marketMorningAdminApi.listEventBriefs("auto_validated", 20);
    await marketMorningAdminApi.reviewEventBrief(
      "brief-1",
      "reject",
      "unsupported_claim",
    );

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/market-morning/_internal/event-briefs?review_status=auto_validated&limit=20",
    );
    const reviewInit = fetchMock.mock.calls[1][1] as RequestInit;
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/market-morning/_internal/event-briefs/brief-1/review",
    );
    expect(reviewInit.method).toBe("POST");
    expect(JSON.parse(String(reviewInit.body))).toEqual({
      decision: "reject",
      reason_code: "unsupported_claim",
    });
    expect(String(reviewInit.body)).not.toContain("payload");
  });

  it("lists and resolves privacy-safe content reports", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: [], count: 0 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            status: "reviewed",
            report_id: "88888888-8888-4888-8888-888888888888",
            report_status: "resolved",
            resolution_code: "content_revised",
            reviewed_at: "2026-07-21T01:10:00Z",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const { marketMorningAdminApi } = await loadApi();

    await marketMorningAdminApi.listContentReports("pending", 50);
    await marketMorningAdminApi.reviewContentReport(
      "88888888-8888-4888-8888-888888888888",
      "resolve",
      "content_revised",
    );

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/market-morning/_internal/content-reports?report_status=pending&limit=50",
    );
    const [path, options] = fetchMock.mock.calls[1];
    expect(path).toBe(
      "/market-morning/_internal/content-reports/88888888-8888-4888-8888-888888888888/review",
    );
    expect(options).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          decision: "resolve",
          resolution_code: "content_revised",
        }),
      }),
    );
    expect(String(options.body)).not.toContain("user_id");
    expect(String(options.body)).not.toContain("payload");
  });

  it("lists and reviews aliases and merge candidates without source content", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ items: [], count: 0, status: "reviewed" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { marketMorningAdminApi } = await loadApi();

    await marketMorningAdminApi.listIssuerAliases("pending", 20);
    await marketMorningAdminApi.reviewIssuerAlias(
      "alias-1",
      "approve",
      "verified_company_name",
    );
    await marketMorningAdminApi.listEventMergeCandidates("pending", 30);
    await marketMorningAdminApi.reviewEventMergeCandidate(
      "candidate-1",
      "reject",
      "distinct_events",
    );

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/market-morning/_internal/issuer-aliases?review_status=pending&limit=20",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/market-morning/_internal/issuer-aliases/alias-1/review",
    );
    expect(JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body))).toEqual({
      decision: "approve",
      reason_code: "verified_company_name",
    });
    expect(fetchMock.mock.calls[2][0]).toBe(
      "/market-morning/_internal/event-merge-candidates?review_status=pending&limit=30",
    );
    expect(fetchMock.mock.calls[3][0]).toBe(
      "/market-morning/_internal/event-merge-candidates/candidate-1/review",
    );
    const mergeBody = String((fetchMock.mock.calls[3][1] as RequestInit).body);
    expect(JSON.parse(mergeBody)).toEqual({
      decision: "reject",
      reason_code: "distinct_events",
    });
    expect(mergeBody).not.toContain("payload");
    expect(mergeBody).not.toContain("source_url");
  });

  it("creates and revokes only database-backed publication halts", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ status: "created", override: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { marketMorningAdminApi } = await loadApi();

    await marketMorningAdminApi.createPublicationHalt(
      "2026-07-22",
      "content_quality_incident",
    );
    await marketMorningAdminApi.revokePublicationHalt("2026-07-22");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/market-morning/_internal/operations/halts",
    );
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      edition_date: "2026-07-22",
      reason_code: "content_quality_incident",
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/market-morning/_internal/operations/halts/2026-07-22",
    );
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("DELETE");
  });

  it("creates hash-only beta invites and mutates access through explicit admin routes", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ status: "created" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { marketMorningAdminApi } = await loadApi();

    await marketMorningAdminApi.createPrivateBetaInvite(7);
    await marketMorningAdminApi.revokePrivateBetaInvite("invite-1");
    await marketMorningAdminApi.setUserAccess(
      "user-1",
      "suspend",
      "beta_access_revoked",
    );

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/market-morning/_internal/private-beta/invites",
    );
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({
      expires_in_days: 7,
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/market-morning/_internal/private-beta/invites/invite-1",
    );
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("DELETE");
    expect(fetchMock.mock.calls[2][0]).toBe(
      "/market-morning/_internal/users/user-1/access",
    );
    expect(JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body))).toEqual({
      action: "suspend",
      reason_code: "beta_access_revoked",
    });
  });
});
