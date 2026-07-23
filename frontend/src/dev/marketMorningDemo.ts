import type {
  IssuerResearchResponse,
  MarketMorningSettings,
  MorningEdition,
  WatchlistItem,
} from "@/lib/marketMorningApi";

const TOYOTA_ID = "22222222-2222-4222-8222-222222222222";
const SONY_ID = "33333333-3333-4333-8333-333333333333";
const SOFTBANK_ID = "44444444-4444-4444-8444-444444444444";
const NTT_ID = "55555555-5555-4555-8555-555555555555";

interface DemoState {
  settings: MarketMorningSettings;
  watchlist: WatchlistItem[];
  noteText: string;
}

export interface DemoRequest {
  body?: unknown;
  method: string;
  url: string;
}

export interface DemoResponse {
  body: unknown;
  status: number;
}

export const MARKET_MORNING_DEV_API_PROXY_PATTERN =
  "^/market-morning/(?:_internal/(?:auth/session|deployment-preflight|operations/(?:summary|halts(?:/[^/]+)?)|event-briefs(?:/[^/]+/review)?|content-reports(?:/[^/]+/review)?|issuer-aliases(?:/[^/]+/review)?|event-merge-candidates(?:/[^/]+/review)?|private-beta/invites(?:/[^/]+)?|users/[^/]+/access)|auth/session|private-beta/invitations/accept|delivery-links/redeem|settings|edition/today|editions/[^/]+/(?:event-states|events/[^/]+/(?:state|report)|sources/open)|watchlist(?:/[^/]+)?|issuers/(?:search|[^/]+/(?:research|note))|consents|account-deletion-requests|account-data-export)(?:\\?[^#]*)?$";

function watchlistItem(
  issuerId: string,
  issuerCode: string,
  legalNameJa: string,
  sortOrder: number,
): WatchlistItem {
  return {
    watchlist_item_id: `aaaaaaaa-aaaa-4aaa-8aaa-${String(sortOrder + 1).padStart(12, "0")}`,
    issuer_id: issuerId,
    issuer_code: issuerCode,
    legal_name_ja: legalNameJa,
    market_segment: "プライム",
    issuer_status: "active",
    user_label: null,
    sort_order: sortOrder,
    created_at: "2026-07-21T23:00:00Z",
  };
}

const INITIAL_WATCHLIST = [
  watchlistItem(TOYOTA_ID, "7203", "トヨタ自動車株式会社", 0),
  watchlistItem(SONY_ID, "6758", "ソニーグループ株式会社", 1),
  watchlistItem(SOFTBANK_ID, "9984", "ソフトバンクグループ株式会社", 2),
];

const EDITION: MorningEdition = {
  edition_date: "2026-07-22",
  generated_at: "2026-07-21T22:00:00Z",
  status: "partial",
  day_plan: {
    edition_date: "2026-07-22",
    generate: true,
    status: "scheduled",
    overnight_context: "us_session_available",
    reason_code: null,
    us_reason_code: null,
  },
  issuers: [
    {
      issuer_id: TOYOTA_ID,
      issuer_code: "7203",
      legal_name_ja: "トヨタ自動車株式会社",
      status: "ready",
      facts: [
        {
          kind: "fact",
          text: "（訂正）2026年3月期 決算短信",
          event_id: "event-v2",
          event_family_key: "family-7203",
          event_version: 2,
          event_type: "earnings_release",
          occurred_at: "2026-07-21T06:30:00Z",
          lifecycle_status: "corrected",
          citations: [
            {
              provider: "fixture_tdnet",
              document_id: "TD-7203-001",
              revision_key: "v2",
              original_url: "https://example.invalid/tdnet/TD-7203-001-v2.pdf",
              published_at: "2026-07-21T06:30:00Z",
            },
          ],
        },
      ],
      assessment: {
        kind: "inference",
        text: "不足以判断",
        evidence_quality: "insufficient_evidence",
      },
      omitted_event_count: 0,
      warnings: [],
      error_code: null,
    },
    {
      issuer_id: SONY_ID,
      issuer_code: "6758",
      legal_name_ja: "ソニーグループ株式会社",
      status: "unavailable",
      facts: [],
      assessment: {
        kind: "inference",
        text: "不足以判断",
        evidence_quality: "insufficient_evidence",
      },
      omitted_event_count: 0,
      warnings: ["source_unavailable"],
      error_code: "synthetic_source_unavailable",
    },
  ],
  consumed_event_units: 1,
  budget: {
    max_issuers: 10,
    max_events_per_issuer: 5,
    max_total_events: 30,
  },
  budget_exhausted: false,
  omitted_issuer_count: 0,
};

function research(noteText: string): IssuerResearchResponse {
  return {
    issuer_id: TOYOTA_ID,
    issuer_code: "7203",
    legal_name_ja: "トヨタ自動車株式会社",
    market_segment: "プライム",
    note: noteText
      ? {
          note_id: "66666666-6666-4666-8666-666666666666",
          text: noteText,
          created_at: "2026-07-22T00:20:00Z",
          updated_at: "2026-07-22T00:40:00Z",
        }
      : null,
    events: [
      {
        event_id: "77777777-7777-4777-8777-777777777777",
        event_family_key: "tdnet:TD-7203-001",
        event_version: 2,
        title: "（訂正）業績予想の修正",
        event_type: "guidance_revision",
        occurred_at: "2026-07-22T00:30:00Z",
        lifecycle_status: "corrected",
        supersedes_event_id: "77777777-7777-4777-8777-666666666666",
        facts: [
          {
            text: "通期売上高予想を修正した。",
            citations: [
              {
                provider: "fixture_tdnet",
                document_id: "TD-7203-001",
                revision_key: "v2",
                original_url: "https://example.invalid/tdnet/TD-7203-001-v2.pdf",
                published_at: "2026-07-22T00:30:00Z",
              },
            ],
          },
        ],
        citations: [
          {
            provider: "fixture_tdnet",
            document_id: "TD-7203-001",
            revision_key: "v2",
            original_url: "https://example.invalid/tdnet/TD-7203-001-v2.pdf",
            published_at: "2026-07-22T00:30:00Z",
          },
        ],
        warnings: [],
      },
    ],
  };
}

function objectBody(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function watchlistResponse(state: DemoState) {
  return {
    items: state.watchlist,
    active_count: state.watchlist.length,
    limit: 10,
  };
}

export function createMarketMorningDemoState(): DemoState {
  return {
    settings: {
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
    },
    watchlist: INITIAL_WATCHLIST.map((item) => ({ ...item })),
    noteText: "決算説明会で設備投資方針を確認する。",
  };
}

export function isMarketMorningDemoEnabled(
  mode: string,
  value: string | undefined,
): boolean {
  return mode === "development" && value === "true";
}

export function handleMarketMorningDemoRequest(
  state: DemoState,
  request: DemoRequest,
): DemoResponse | null {
  const method = request.method.toUpperCase();
  const url = new URL(request.url, "http://market-morning-demo.invalid");
  const path = url.pathname;

  if (method === "GET" && path === "/market-morning/settings") {
    return { status: 200, body: state.settings };
  }
  if (method === "PATCH" && path === "/market-morning/settings") {
    const body = objectBody(request.body);
    state.settings = {
      ...state.settings,
      ...(typeof body.timezone === "string" ? { timezone: body.timezone } : {}),
      ...(typeof body.email_opt_in === "boolean"
        ? { email_opt_in: body.email_opt_in }
        : {}),
    };
    return { status: 200, body: state.settings };
  }
  if (method === "GET" && path === "/market-morning/watchlist") {
    return { status: 200, body: watchlistResponse(state) };
  }
  if (method === "GET" && path === "/market-morning/edition/today") {
    return {
      status: 200,
      body: {
        status: "partial",
        data_mode: "synthetic_fixture",
        edition_id: null,
        reason_code: null,
        fixture_version: "local-browser-demo-v1",
        edition: EDITION,
      },
    };
  }
  if (method === "GET" && path === "/market-morning/issuers/search") {
    const query = (url.searchParams.get("q") ?? "").trim();
    const matched = query === "9432" || query.includes("NTT");
    return {
      status: 200,
      body: {
        query,
        normalized_query: query,
        items: matched
          ? [
              {
                issuer_id: NTT_ID,
                issuer_code: "9432",
                legal_name_ja: "日本電信電話株式会社",
                market_segment: "プライム",
                match_kind: query === "9432" ? "code_exact" : "official_prefix",
                matched_alias: null,
              },
            ]
          : [],
        no_match_guidance: matched
          ? null
          : "会社名または4文字の証券コードを確認してください。",
      },
    };
  }
  if (method === "POST" && path === "/market-morning/watchlist") {
    const issuerId = objectBody(request.body).issuer_id;
    if (issuerId !== NTT_ID) {
      return { status: 404, body: { detail: "demo_issuer_not_found" } };
    }
    const current = state.watchlist.find((item) => item.issuer_id === NTT_ID);
    if (current) {
      return {
        status: 200,
        body: {
          status: "already_active",
          item: current,
          active_count: state.watchlist.length,
          limit: 10,
        },
      };
    }
    const item = watchlistItem(
      NTT_ID,
      "9432",
      "日本電信電話株式会社",
      state.watchlist.length,
    );
    state.watchlist.push(item);
    return {
      status: 200,
      body: {
        status: "added",
        item,
        active_count: state.watchlist.length,
        limit: 10,
      },
    };
  }
  const watchlistMatch = path.match(/^\/market-morning\/watchlist\/([^/]+)$/);
  if (method === "DELETE" && watchlistMatch) {
    const issuerId = decodeURIComponent(watchlistMatch[1]);
    const index = state.watchlist.findIndex((item) => item.issuer_id === issuerId);
    const [item] = index >= 0 ? state.watchlist.splice(index, 1) : [null];
    return {
      status: 200,
      body: {
        status: item ? "removed" : "already_removed",
        item,
        active_count: state.watchlist.length,
        limit: 10,
      },
    };
  }
  if (
    method === "GET" &&
    path === `/market-morning/issuers/${TOYOTA_ID}/research`
  ) {
    return { status: 200, body: research(state.noteText) };
  }
  if (path === `/market-morning/issuers/${TOYOTA_ID}/note`) {
    if (method === "PUT") {
      const text = objectBody(request.body).text;
      state.noteText = typeof text === "string" ? text : "";
      return {
        status: 200,
        body: { status: "saved", note: research(state.noteText).note },
      };
    }
    if (method === "DELETE") {
      state.noteText = "";
      return { status: 200, body: { status: "cleared" } };
    }
  }
  if (method === "GET" && path === "/market-morning/account-data-export") {
    return {
      status: 200,
      body: {
        schema_version: 1,
        generated_at: "2026-07-22T01:30:00Z",
        data: {
          account: [{ account_status: "active" }],
          watchlist: state.watchlist,
          settings: [state.settings],
        },
      },
    };
  }
  return null;
}
