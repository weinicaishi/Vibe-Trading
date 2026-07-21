import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import {
  clearMarketMorningAuthLifecycleAdapters,
  clearMarketMorningBearerTokenProviders,
  setMarketMorningAuthLifecycleAdapter,
} from "@/lib/marketMorningAuth";
import {
  MarketMorningApiError,
  marketMorningApi,
  type MarketMorningSettings,
  type IssuerResearchResponse,
  type WatchlistResponse,
} from "@/lib/marketMorningApi";
import { MarketMorningShell } from "@/pages/market-morning/MarketMorningShell";
import {
  EditionStatusPanel,
  MarketMorningToday,
} from "@/pages/market-morning/MarketMorningToday";
import { MarketMorningWatchlist } from "@/pages/market-morning/MarketMorningWatchlist";
import { MarketMorningSettings as MarketMorningSettingsPage } from "@/pages/market-morning/MarketMorningSettings";
import { MarketMorningIssuerResearch } from "@/pages/market-morning/MarketMorningIssuerResearch";

const SETTINGS: MarketMorningSettings = {
  timezone: "Asia/Tokyo",
  email_opt_in: false,
  risk_disclosure: {
    consent_type: "risk_disclosure",
    accepted: false,
    consent_version: null,
    accepted_at: null,
    revoked_at: null,
  },
  data_disclosure: {
    consent_type: "data_disclosure",
    accepted: false,
    consent_version: null,
    accepted_at: null,
    revoked_at: null,
  },
};

const EMPTY_WATCHLIST: WatchlistResponse = { items: [], active_count: 0, limit: 10 };

const READY_WATCHLIST: WatchlistResponse = {
  active_count: 3,
  limit: 10,
  items: ["7203", "6758", "9984"].map((issuerCode, index) => ({
    watchlist_item_id: `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa${index}`,
    issuer_id: `${index + 2}2222222-2222-4222-8222-222222222222`,
    issuer_code: issuerCode,
    legal_name_ja: `発行体 ${issuerCode}`,
    market_segment: "プライム",
    issuer_status: "active",
    user_label: null,
    sort_order: index,
    created_at: "2026-07-20T23:00:00Z",
  })),
};

const SYNTHETIC_EDITION = {
  status: "partial" as const,
  data_mode: "synthetic_fixture" as const,
  edition_id: null,
  reason_code: null,
  fixture_version: "synthetic-v1",
  edition: {
    edition_date: "2026-07-21",
    generated_at: "2026-07-20T22:00:00Z",
    status: "partial" as const,
    day_plan: {
      edition_date: "2026-07-21",
      generate: true,
      status: "scheduled" as const,
      overnight_context: "us_session_available" as const,
      reason_code: null,
      us_reason_code: null,
    },
    issuers: [
      {
        issuer_id: "22222222-2222-4222-8222-222222222222",
        issuer_code: "7203",
        legal_name_ja: "トヨタ自動車株式会社",
        status: "ready" as const,
        facts: [
          {
            kind: "fact" as const,
            text: "（訂正）2026年3月期 決算短信",
            event_id: "event-v2",
            event_family_key: "family-7203",
            event_version: 2,
            event_type: "earnings_release",
            occurred_at: "2026-07-20T06:30:00Z",
            lifecycle_status: "corrected" as const,
            citations: [
              {
                provider: "fixture_tdnet",
                document_id: "TD-7203-001",
                revision_key: "v2",
                original_url: "https://example.invalid/tdnet/TD-7203-001-v2.pdf",
                published_at: "2026-07-20T06:30:00Z",
              },
            ],
          },
        ],
        assessment: {
          kind: "inference" as const,
          text: "不足以判断",
          evidence_quality: "insufficient_evidence" as const,
        },
        omitted_event_count: 0,
        warnings: [],
        error_code: null,
      },
      {
        issuer_id: "33333333-3333-4333-8333-333333333333",
        issuer_code: "6758",
        legal_name_ja: "ソニーグループ株式会社",
        status: "unavailable" as const,
        facts: [],
        assessment: {
          kind: "inference" as const,
          text: "不足以判断",
          evidence_quality: "insufficient_evidence" as const,
        },
        omitted_event_count: 0,
        warnings: ["source_unavailable"],
        error_code: "synthetic_source_unavailable",
      },
    ],
    consumed_event_units: 1,
    budget: { max_issuers: 10, max_events_per_issuer: 5, max_total_events: 30 },
    budget_exhausted: false,
    omitted_issuer_count: 0,
  },
};

const ISSUER_RESEARCH: IssuerResearchResponse = {
  issuer_id: "22222222-2222-4222-8222-222222222222",
  issuer_code: "7203",
  legal_name_ja: "トヨタ自動車株式会社",
  market_segment: "プライム",
  note: {
    note_id: "55555555-5555-4555-8555-555555555555",
    text: "決算説明会で設備投資方針を確認する。",
    created_at: "2026-07-21T00:20:00Z",
    updated_at: "2026-07-21T00:20:00Z",
  },
  events: [
    {
      event_id: "33333333-3333-4333-8333-333333333333",
      event_family_key: "tdnet:TD-7203-001",
      event_version: 2,
      title: "（訂正）業績予想の修正",
      event_type: "guidance_revision",
      occurred_at: "2026-07-21T00:30:00Z",
      lifecycle_status: "corrected",
      supersedes_event_id: "33333333-3333-4333-8333-222222222222",
      facts: [
        {
          text: "通期売上高予想を修正した。",
          citations: [
            {
              provider: "tdnet",
              document_id: "TD-7203-001",
              revision_key: "v2",
              original_url: "https://example.jp/tdnet/TD-7203-001-v2.pdf",
              published_at: "2026-07-21T00:30:00Z",
            },
          ],
        },
      ],
      citations: [
        {
          provider: "tdnet",
          document_id: "TD-7203-001",
          revision_key: "v2",
          original_url: "https://example.jp/tdnet/TD-7203-001-v2.pdf",
          published_at: "2026-07-21T00:30:00Z",
        },
      ],
      warnings: [],
    },
  ],
};

function renderProduct(initialEntry = "/market-morning") {
  const router = createMemoryRouter(
    [
      {
        path: "/market-morning",
        element: <MarketMorningShell />,
        children: [
          { index: true, element: <MarketMorningToday /> },
          { path: "app/watchlist", element: <MarketMorningWatchlist /> },
          { path: "app/issuers/:issuerId", element: <MarketMorningIssuerResearch /> },
          { path: "app/settings", element: <MarketMorningSettingsPage /> },
        ],
      },
      { path: "/", element: <div>Vibe home</div> },
    ],
    { initialEntries: [initialEntry] },
  );
  return render(<RouterProvider router={router} />);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  clearMarketMorningAuthLifecycleAdapters();
  clearMarketMorningBearerTokenProviders();
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("Market Morning private product shell", () => {
  it("clears and redeems an email fragment token exactly once", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(EMPTY_WATCHLIST);
    const redeemSpy = vi
      .spyOn(marketMorningApi, "redeemDeliveryLink")
      .mockResolvedValue({
        status: "accepted",
        edition_date: "2026-07-21",
        destination_path: "/market-morning",
      });
    const token = "A".repeat(43);
    window.history.replaceState(null, "", `/market-morning#token=${token}`);

    renderProduct();

    await waitFor(() => expect(redeemSpy).toHaveBeenCalledOnce());
    expect(redeemSpy).toHaveBeenCalledWith(token);
    expect(window.location.hash).toBe("");
    expect(JSON.stringify(window.localStorage)).not.toContain(token);
    expect(JSON.stringify(window.sessionStorage)).not.toContain(token);
  });

  it("initializes the deployment auth lifecycle before private product APIs", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    const order: string[] = [];
    setMarketMorningAuthLifecycleAdapter("product", {
      initialize: async () => {
        order.push("initialize");
      },
      getAccessToken: () => "product-token",
      login: vi.fn(),
      logout: vi.fn(),
    });
    vi.spyOn(marketMorningApi, "getSettings").mockImplementation(async () => {
      order.push("settings");
      return SETTINGS;
    });
    vi.spyOn(marketMorningApi, "getWatchlist").mockImplementation(async () => {
      order.push("watchlist");
      return EMPTY_WATCHLIST;
    });

    renderProduct();

    expect(await screen.findByText("まず、3銘柄を選びます。")).toBeInTheDocument();
    expect(order[0]).toBe("initialize");
    expect(order.slice(1).sort()).toEqual(["settings", "watchlist"]);
  });

  it("starts login, reloads in place, and only then redeems an in-memory email token", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    const authError = new MarketMorningApiError("Authentication required", 401);
    const login = vi.fn();
    setMarketMorningAuthLifecycleAdapter("product", {
      initialize: vi.fn(),
      getAccessToken: () => null,
      login,
      logout: vi.fn(),
    });
    vi.spyOn(marketMorningApi, "getSettings")
      .mockRejectedValueOnce(authError)
      .mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist")
      .mockRejectedValueOnce(authError)
      .mockResolvedValue(EMPTY_WATCHLIST);
    const redeemSpy = vi
      .spyOn(marketMorningApi, "redeemDeliveryLink")
      .mockResolvedValue({
        status: "accepted",
        edition_date: "2026-07-21",
        destination_path: "/market-morning",
      });
    const token = "B".repeat(43);
    window.history.replaceState(null, "", `/market-morning#token=${token}`);
    const user = userEvent.setup();

    renderProduct();

    expect(await screen.findByText("ログインが必要です")).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(redeemSpy).not.toHaveBeenCalled();
    expect(screen.getByText(/メールの専用リンクをもう一度開いてください/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "ログイン" }));

    expect(await screen.findByText("まず、3銘柄を選びます。")).toBeInTheDocument();
    expect(login).toHaveBeenCalledWith("/market-morning");
    await waitFor(() => expect(redeemSpy).toHaveBeenCalledWith(token));
    expect(JSON.stringify(window.localStorage)).not.toContain(token);
    expect(JSON.stringify(window.sessionStorage)).not.toContain(token);
  });

  it("offers logout only for a configured authenticated lifecycle", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    const logout = vi.fn();
    setMarketMorningAuthLifecycleAdapter("product", {
      initialize: vi.fn(),
      getAccessToken: () => "product-token",
      login: vi.fn(),
      logout,
    });
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(EMPTY_WATCHLIST);
    const user = userEvent.setup();

    renderProduct();

    await user.click(await screen.findByRole("button", { name: "ログアウト" }));
    expect(logout).toHaveBeenCalledWith("/");
    expect(await screen.findByText("ログインが必要です")).toBeInTheDocument();
    expect(screen.queryByText("まず、3銘柄を選びます。")).not.toBeInTheDocument();
  });

  it("fails closed when auth initialization fails and supports a clean retry", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    const initialize = vi
      .fn()
      .mockRejectedValueOnce(new Error("private provider failure"))
      .mockResolvedValueOnce(undefined);
    setMarketMorningAuthLifecycleAdapter("product", {
      initialize,
      getAccessToken: () => "product-token",
      login: vi.fn(),
      logout: vi.fn(),
    });
    const settingsSpy = vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    const watchlistSpy = vi
      .spyOn(marketMorningApi, "getWatchlist")
      .mockResolvedValue(EMPTY_WATCHLIST);
    const user = userEvent.setup();

    renderProduct();

    expect(await screen.findByText("ログイン状態を初期化できませんでした。")).toBeInTheDocument();
    expect(screen.queryByText("private provider failure")).not.toBeInTheDocument();
    expect(settingsSpy).not.toHaveBeenCalled();
    expect(watchlistSpy).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "状態を再確認" }));

    expect(await screen.findByText("まず、3銘柄を選びます。")).toBeInTheDocument();
    expect(initialize).toHaveBeenCalledTimes(2);
  });

  it("does not access product APIs while the UI feature flag is closed", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "false");
    const settingsSpy = vi.spyOn(marketMorningApi, "getSettings");
    const watchlistSpy = vi.spyOn(marketMorningApi, "getWatchlist");

    renderProduct();

    expect(await screen.findByText("Market Morning は現在オフです")).toBeInTheDocument();
    expect(settingsSpy).not.toHaveBeenCalled();
    expect(watchlistSpy).not.toHaveBeenCalled();
  });

  it("shows an honest access gate when product authentication is not configured", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    const authError = new MarketMorningApiError(
      "Market Morning product authentication is not configured",
      503,
    );
    vi.spyOn(marketMorningApi, "getSettings").mockRejectedValue(authError);
    vi.spyOn(marketMorningApi, "getWatchlist").mockRejectedValue(authError);

    renderProduct();

    expect(await screen.findByText("招待制ログインを準備中です")).toBeInTheDocument();
    expect(screen.queryByText("まず、3銘柄を選びます。")).not.toBeInTheDocument();
  });

  it("starts onboarding only after settings and watchlist authentication succeeds", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(EMPTY_WATCHLIST);
    const editionSpy = vi.spyOn(marketMorningApi, "getTodayEdition");

    renderProduct();

    expect(await screen.findByText("まず、3銘柄を選びます。")).toBeInTheDocument();
    expect(screen.getByText("0", { selector: "p" })).toBeInTheDocument();
    expect(screen.queryByText(/Buy|Sell|Hold|おすすめ/i)).not.toBeInTheDocument();
    expect(editionSpy).not.toHaveBeenCalled();
  });

  it("marks every private product page as noindex", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(EMPTY_WATCHLIST);

    renderProduct();

    expect(await screen.findByText("まず、3銘柄を選びます。")).toBeInTheDocument();
    expect(document.querySelector('meta[name="robots"]')).toHaveAttribute(
      "content",
      "noindex,nofollow,noarchive",
    );
  });

  it("renders a clearly labeled synthetic edition with facts, citations, and gaps", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(READY_WATCHLIST);
    vi.spyOn(marketMorningApi, "getTodayEdition").mockResolvedValue(SYNTHETIC_EDITION);

    renderProduct();

    expect(await screen.findByText("デモデータ")).toBeInTheDocument();
    expect(screen.getByText("一部の情報源を確認できません")).toBeInTheDocument();
    expect(screen.getByText("（訂正）2026年3月期 決算短信")).toBeInTheDocument();
    expect(screen.getByText("訂正")).toBeInTheDocument();
    expect(screen.getByText("TDnet（デモ）")).toBeInTheDocument();
    expect(screen.getAllByText("不足以判断").length).toBeGreaterThan(0);
    expect(screen.getByText("データ取得不可")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "内容を報告" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Buy|Sell|Hold|おすすめ/i)).not.toBeInTheDocument();
  });

  it("renders persisted editions without the synthetic demo banner", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(READY_WATCHLIST);
    vi.spyOn(marketMorningApi, "getTodayEdition").mockResolvedValue({
      ...SYNTHETIC_EDITION,
      data_mode: "persisted",
      edition_id: "22222222-2222-4222-8222-222222222222",
      fixture_version: null,
    });
    vi.spyOn(marketMorningApi, "getEditionEventStates").mockResolvedValue({
      items: [],
    });

    renderProduct();

    expect(
      await screen.findByText("（訂正）2026年3月期 決算短信"),
    ).toBeInTheDocument();
    expect(screen.queryByText("デモデータ")).not.toBeInTheDocument();
  });

  it("restores and updates private event state and audits source opens", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    const editionId = "22222222-2222-4222-8222-222222222222";
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(READY_WATCHLIST);
    vi.spyOn(marketMorningApi, "getTodayEdition").mockResolvedValue({
      ...SYNTHETIC_EDITION,
      data_mode: "persisted",
      edition_id: editionId,
      fixture_version: null,
    });
    vi.spyOn(marketMorningApi, "getEditionEventStates").mockResolvedValue({
      items: [
        {
          edition_id: editionId,
          event_id: "event-v2",
          state: "later",
          first_read_at: null,
          updated_at: "2026-07-21T00:30:00Z",
        },
      ],
    });
    const updateSpy = vi
      .spyOn(marketMorningApi, "updateEditionEventState")
      .mockResolvedValue({
        edition_id: editionId,
        event_id: "event-v2",
        state: "read",
        first_read_at: "2026-07-21T00:31:00Z",
        updated_at: "2026-07-21T00:31:00Z",
      });
    const sourceSpy = vi
      .spyOn(marketMorningApi, "recordEditionSourceOpen")
      .mockResolvedValue({
        status: "recorded",
        source_open_id: "66666666-6666-4666-8666-666666666666",
        original_url: "https://example.invalid/tdnet/TD-7203-001-v2.pdf",
      });
    const user = userEvent.setup();

    renderProduct();

    const later = await screen.findByRole("button", { name: "あとで" });
    await waitFor(() => expect(later).toHaveAttribute("aria-pressed", "true"));
    await user.click(screen.getByRole("button", { name: "既読" }));
    expect(updateSpy).toHaveBeenCalledWith(editionId, "event-v2", "read");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "既読" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );

    await user.click(screen.getByRole("link", { name: /TDnet（デモ）/ }));
    expect(sourceSpy).toHaveBeenCalledWith(
      editionId,
      expect.objectContaining({
        event_id: "event-v2",
        provider: "fixture_tdnet",
        document_id: "TD-7203-001",
        revision_key: "v2",
        request_id: expect.any(String),
      }),
    );
    expect(sourceSpy.mock.calls[0][1]).not.toHaveProperty("original_url");
  });

  it("reports a persisted event using only a fixed reason", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    const editionId = "22222222-2222-4222-8222-222222222222";
    const eventId = "33333333-3333-4333-8333-333333333333";
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(READY_WATCHLIST);
    vi.spyOn(marketMorningApi, "getTodayEdition").mockResolvedValue({
      ...SYNTHETIC_EDITION,
      data_mode: "persisted",
      edition_id: editionId,
      fixture_version: null,
      edition: {
        ...SYNTHETIC_EDITION.edition,
        issuers: [
          {
            ...SYNTHETIC_EDITION.edition.issuers[0],
            facts: [
              {
                ...SYNTHETIC_EDITION.edition.issuers[0].facts[0],
                event_id: eventId,
              },
            ],
          },
        ],
      },
    });
    vi.spyOn(marketMorningApi, "getEditionEventStates").mockResolvedValue({
      items: [],
    });
    const reportSpy = vi
      .spyOn(marketMorningApi, "reportEditionEvent")
      .mockResolvedValue({
        status: "reported",
        report_id: "88888888-8888-4888-8888-888888888888",
        reason_code: "source_mismatch",
        report_status: "pending",
        created_at: "2026-07-21T01:00:00Z",
      });
    const user = userEvent.setup();

    renderProduct();

    await user.click(await screen.findByRole("button", { name: "内容を報告" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "報告理由" }),
      "source_mismatch",
    );
    await user.click(screen.getByRole("button", { name: "報告を送信" }));

    await waitFor(() =>
      expect(reportSpy).toHaveBeenCalledWith(
        editionId,
        eventId,
        "source_mismatch",
      ),
    );
    expect(await screen.findByText("報告済み")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("renders one interaction control set for multiple facts from one event", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(READY_WATCHLIST);
    vi.spyOn(marketMorningApi, "getTodayEdition").mockResolvedValue({
      ...SYNTHETIC_EDITION,
      data_mode: "persisted",
      edition_id: "22222222-2222-4222-8222-222222222222",
      fixture_version: null,
      edition: {
        ...SYNTHETIC_EDITION.edition,
        issuers: [
          {
            ...SYNTHETIC_EDITION.edition.issuers[0],
            facts: [
              SYNTHETIC_EDITION.edition.issuers[0].facts[0],
              {
                ...SYNTHETIC_EDITION.edition.issuers[0].facts[0],
                text: "営業利益予想も修正した。",
              },
            ],
          },
        ],
      },
    });
    vi.spyOn(marketMorningApi, "getEditionEventStates").mockResolvedValue({
      items: [],
    });

    renderProduct();

    expect(await screen.findByText("営業利益予想も修正した。")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "既読" })).toHaveLength(1);
  });

  it("searches and adds an issuer without storing a private label", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(EMPTY_WATCHLIST);
    vi.spyOn(marketMorningApi, "searchIssuers").mockResolvedValue({
      query: "7203",
      normalized_query: "7203",
      no_match_guidance: null,
      items: [
        {
          issuer_id: "22222222-2222-4222-8222-222222222222",
          issuer_code: "7203",
          legal_name_ja: "トヨタ自動車株式会社",
          market_segment: "プライム",
          match_kind: "code_exact",
          matched_alias: null,
        },
      ],
    });
    const addSpy = vi.spyOn(marketMorningApi, "addWatchlistItem").mockResolvedValue({
      status: "added",
      active_count: 1,
      limit: 10,
      item: {
        watchlist_item_id: "33333333-3333-4333-8333-333333333333",
        issuer_id: "22222222-2222-4222-8222-222222222222",
        issuer_code: "7203",
        legal_name_ja: "トヨタ自動車株式会社",
        market_segment: "プライム",
        issuer_status: "active",
        user_label: null,
        sort_order: 0,
        created_at: "2026-07-20T23:00:00",
      },
    });
    const user = userEvent.setup();
    renderProduct("/market-morning/app/watchlist");

    const input = await screen.findByLabelText("会社名または証券コード");
    await user.type(input, "7203");
    await user.click(screen.getByRole("button", { name: "検索" }));
    await user.click(await screen.findByRole("button", { name: "追加" }));

    expect(addSpy).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222");
    expect(await screen.findByText("1 / 10")).toBeInTheDocument();
  });

  it("shows a watched issuer history with verified facts and a lightweight note", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(READY_WATCHLIST);
    vi.spyOn(marketMorningApi, "getIssuerResearch").mockResolvedValue(
      ISSUER_RESEARCH,
    );
    const updateSpy = vi.spyOn(marketMorningApi, "updateIssuerNote").mockResolvedValue({
      status: "saved",
      note: {
        ...ISSUER_RESEARCH.note!,
        text: "次回の開示で海外販売台数を確認する。",
        updated_at: "2026-07-21T00:40:00Z",
      },
    });
    const user = userEvent.setup();

    renderProduct(
      "/market-morning/app/issuers/22222222-2222-4222-8222-222222222222",
    );

    expect(await screen.findByText("トヨタ自動車株式会社")).toBeInTheDocument();
    expect(screen.getByText("（訂正）業績予想の修正")).toBeInTheDocument();
    expect(screen.getByText("通期売上高予想を修正した。")).toBeInTheDocument();
    expect(screen.getByText("訂正済み")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /TDnet/ })).toHaveAttribute(
      "href",
      "https://example.jp/tdnet/TD-7203-001-v2.pdf",
    );

    const note = screen.getByRole("textbox", { name: "自分用メモ" });
    await user.clear(note);
    await user.type(note, "次回の開示で海外販売台数を確認する。");
    await user.click(screen.getByRole("button", { name: "メモを保存" }));

    expect(updateSpy).toHaveBeenCalledWith(
      "22222222-2222-4222-8222-222222222222",
      "次回の開示で海外販売台数を確認する。",
    );
    expect(await screen.findByText("保存しました")).toBeInTheDocument();
    expect(screen.queryByText(/Buy|Sell|Hold|おすすめ/i)).not.toBeInTheDocument();
  });

  it("links every active watchlist issuer to its private research page", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(READY_WATCHLIST);

    renderProduct("/market-morning/app/watchlist");

    const link = await screen.findByRole("link", { name: "発行体 7203の研究を見る" });
    expect(link).toHaveAttribute(
      "href",
      "/market-morning/app/issuers/22222222-2222-4222-8222-222222222222",
    );
  });

  it("saves email opt-in and keeps legal consent disabled until versions exist", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(EMPTY_WATCHLIST);
    const updateSpy = vi.spyOn(marketMorningApi, "updateSettings").mockResolvedValue({
      ...SETTINGS,
      email_opt_in: true,
    });
    const user = userEvent.setup();
    renderProduct("/market-morning/app/settings");

    const checkbox = await screen.findByRole("checkbox", { name: "メール通知を受け取る" });
    await user.click(checkbox);
    await user.click(screen.getByRole("button", { name: "設定を保存" }));

    expect(updateSpy).toHaveBeenCalledWith({ email_opt_in: true });
    expect(await screen.findByText("保存しました")).toBeInTheDocument();
    expect(screen.getByText(/正式な説明文とサーバー側の現行バージョン/)).toBeInTheDocument();
  });

  it("downloads an authenticated account-data export before deletion", async () => {
    vi.stubEnv("VITE_MARKET_MORNING_ENABLED", "true");
    vi.spyOn(marketMorningApi, "getSettings").mockResolvedValue(SETTINGS);
    vi.spyOn(marketMorningApi, "getWatchlist").mockResolvedValue(EMPTY_WATCHLIST);
    const exportSpy = vi.spyOn(marketMorningApi, "exportAccountData").mockResolvedValue({
      schema_version: 1,
      generated_at: "2026-07-21T01:30:00Z",
      data: { account: [{ user_id: "user-1" }] },
    });
    const createObjectURL = vi.fn(() => "blob:account-export");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, configurable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: revokeObjectURL, configurable: true });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const user = userEvent.setup();
    renderProduct("/market-morning/app/settings");

    await user.click(await screen.findByRole("button", { name: "データを書き出す" }));

    expect(exportSpy).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("データを書き出しました")).toBeInTheDocument();
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:account-export");
  });
});

describe("edition empty states", () => {
  it.each([
    ["no_data", "確認できるデータがありません"],
    ["market_holiday", "本日は東京市場の休場日です"],
    ["not_published", "本日の朝刊はまだ公開されていません"],
    ["partial", "一部の情報だけ確認できています"],
  ] as const)("renders %s as a distinct state", (state, title) => {
    render(<EditionStatusPanel state={state} />);
    expect(screen.getByText(title)).toBeInTheDocument();
    expect(screen.queryByText("重要な変化なし")).not.toBeInTheDocument();
  });
});
