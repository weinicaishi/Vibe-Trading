import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Database,
  Flag,
  Loader2,
  LogIn,
  LogOut,
  RefreshCw,
  ShieldCheck,
  UserPlus,
  UserX,
  XCircle,
} from "lucide-react";
import {
  marketMorningAdminApi,
  type DeploymentPreflight,
  type ContentReportQueue,
  type EventMergeReviewQueue,
  type EventBriefReviewQueue,
  type EventBriefReviewStatus,
  type IssuerAliasReviewQueue,
  type OperationsAlert,
  type OperationsSummary,
  type PublicationHaltReason,
  type UserAccessReason,
} from "@/lib/marketMorningAdminApi";
import {
  beginMarketMorningLogin,
  endMarketMorningSession,
  hasMarketMorningAuthLifecycleAdapter,
  initializeMarketMorningAuth,
} from "@/lib/marketMorningAuth";
import { cn } from "@/lib/utils";

const ALERT_LABELS: Record<string, string> = {
  source_error: "公式ソースで最新エラー",
  source_never_run: "未実行の公式ソース",
  job_failures: "失敗した durable job",
  event_brief_blocked: "品質 Gate で停止した EventBrief",
  delivery_failures: "メール配信失敗",
  cost_observability_no_invocations: "モデル呼び出し未観測",
  model_usage_missing: "provider usage 欠損",
  model_cost_missing: "未価格のモデル呼び出し",
};

const COST_STATUS_LABELS: Record<OperationsSummary["cost_observability"], string> = {
  no_invocations: "呼び出し未観測",
  usage_incomplete: "Usage 不完全",
  cost_incomplete: "コスト不完全",
  complete: "計測完了",
};

const DEPLOYMENT_CHECK_LABELS: Record<string, string> = {
  market_morning_disabled: "Market Morning feature が無効です。",
  database_not_ready: "データベース接続または schema revision を確認してください。",
  product_auth_factory_missing: "製品ユーザー認証 factory が未設定です。",
  admin_auth_factory_missing: "個人を識別できる運用認証 factory が未設定です。",
  oidc_issuer_missing: "OIDC issuer が未設定です。",
  oidc_jwks_url_missing: "固定 JWKS URL が未設定です。",
  oidc_audience_missing: "Market Morning API audience が未設定です。",
  oidc_session_validator_factory_missing: "session 撤回確認 factory が未設定です。",
  oidc_session_validator_factory_invalid: "session 撤回確認 factory の module:function 形式を確認してください。",
  oidc_admin_role_permissions_missing: "運用 role の最小権限 mapping が未設定です。",
  oidc_configuration_invalid: "OIDC endpoint、algorithm、timeout 設定を確認してください。",
  oidc_admin_role_permissions_invalid: "運用 role mapping に未許可の権限または不正な形式があります。",
  runtime_disabled: "独立 runtime が無効です。",
  runtime_factory_missing: "deployment runtime factory が未設定です。",
  email_webhook_factory_missing: "メール webhook factory が未設定です。",
  email_identity_factory_missing: "検証済みメール identity factory が未設定です。",
  email_identity_factory_invalid: "メール identity factory の module:function 形式を確認してください。",
  delivery_link_configuration_missing: "私有メールリンクの URL または署名鍵が未設定です。",
  delivery_link_configuration_invalid: "私有メールリンクの URL または署名鍵を確認してください。",
  synthetic_data_enabled: "本番設定で synthetic edition が有効です。",
  fixture_runtime_enabled: "本番設定で fixture runtime が許可されています。",
};

function formatMicros(value: number): string {
  return new Intl.NumberFormat("ja-JP", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 6,
  }).format(value / 1_000_000);
}

const REVIEW_FILTERS: Array<{ value: EventBriefReviewStatus; label: string }> = [
  { value: "auto_validated", label: "自動検証済み" },
  { value: "pending", label: "保留" },
  { value: "approved", label: "承認済み" },
  { value: "rejected", label: "却下済み" },
];

const CONTENT_REPORT_REASON_LABELS: Record<string, string> = {
  fact_inaccurate: "事実が正確ではない",
  source_mismatch: "出典と内容が一致しない",
  outdated_or_corrected: "古い情報・訂正前の内容",
  other_content_issue: "その他の内容上の問題",
};

function isUnauthorized(error: unknown): boolean {
  return (
    error instanceof Error &&
    "status" in error &&
    (error as Error & { status?: unknown }).status === 401
  );
}

function MarketMorningOperationsContent() {
  const [preflight, setPreflight] = useState<DeploymentPreflight | null>(null);
  const [summary, setSummary] = useState<OperationsSummary | null>(null);
  const [queue, setQueue] = useState<EventBriefReviewQueue | null>(null);
  const [contentReports, setContentReports] = useState<ContentReportQueue | null>(null);
  const [issuerAliases, setIssuerAliases] = useState<IssuerAliasReviewQueue | null>(null);
  const [mergeCandidates, setMergeCandidates] = useState<EventMergeReviewQueue | null>(null);
  const [filter, setFilter] = useState<EventBriefReviewStatus>("auto_validated");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [authBusy, setAuthBusy] = useState(false);
  const [signedOut, setSignedOut] = useState(false);
  const [reviewingId, setReviewingId] = useState<string | null>(null);
  const [reviewingReportId, setReviewingReportId] = useState<string | null>(null);
  const [reviewingAliasId, setReviewingAliasId] = useState<string | null>(null);
  const [reviewingCandidateId, setReviewingCandidateId] = useState<string | null>(null);
  const [haltDate, setHaltDate] = useState("");
  const [haltReason, setHaltReason] = useState<PublicationHaltReason>("content_quality_incident");
  const [haltBusy, setHaltBusy] = useState(false);
  const [inviteDays, setInviteDays] = useState(7);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [issuedInvite, setIssuedInvite] = useState<{
    inviteId: string;
    rawToken: string;
    expiresAt: string;
  } | null>(null);
  const [accessUserId, setAccessUserId] = useState("");
  const [accessReason, setAccessReason] = useState<UserAccessReason>("beta_access_revoked");
  const [accessBusy, setAccessBusy] = useState(false);
  const [accessNotice, setAccessNotice] = useState<string | null>(null);

  const load = useCallback(
    async (mode: "initial" | "refresh" = "refresh") => {
      if (mode === "initial") setLoading(true);
      else setRefreshing(true);
      setError(null);
      setAuthRequired(false);
      try {
        const [
          nextPreflight,
          nextSummary,
          nextQueue,
          nextContentReports,
          nextIssuerAliases,
          nextMergeCandidates,
        ] = await Promise.all([
          marketMorningAdminApi.getDeploymentPreflight(),
          marketMorningAdminApi.getSummary(24, 10),
          marketMorningAdminApi.listEventBriefs(filter, 20),
          marketMorningAdminApi.listContentReports("pending", 50),
          marketMorningAdminApi.listIssuerAliases("pending", 50),
          marketMorningAdminApi.listEventMergeCandidates("pending", 50),
        ]);
        setPreflight(nextPreflight);
        setSummary(nextSummary);
        setQueue(nextQueue);
        setContentReports(nextContentReports);
        setIssuerAliases(nextIssuerAliases);
        setMergeCandidates(nextMergeCandidates);
      } catch (caught) {
        setAuthRequired(isUnauthorized(caught));
        setError(
          caught instanceof Error
            ? caught.message
            : "運用データを読み込めませんでした。",
        );
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [filter],
  );

  const login = useCallback(async () => {
    setAuthBusy(true);
    setError(null);
    try {
      await beginMarketMorningLogin("operator", "/market-morning-ops");
      await load("initial");
      setSignedOut(false);
    } catch {
      setError("運用ログインを開始できませんでした。もう一度お試しください。");
    } finally {
      setAuthBusy(false);
    }
  }, [load]);

  const logout = useCallback(async () => {
    setAuthBusy(true);
    setError(null);
    try {
      await endMarketMorningSession("operator", "/");
      setSignedOut(true);
    } catch {
      setError("ログアウトを開始できませんでした。もう一度お試しください。");
    } finally {
      setAuthBusy(false);
    }
  }, []);

  useEffect(() => {
    const previous = document.querySelector<HTMLMetaElement>('meta[name="robots"]');
    const meta = previous ?? document.createElement("meta");
    const previousContent = previous?.content;
    if (!previous) {
      meta.name = "robots";
      document.head.appendChild(meta);
    }
    meta.content = "noindex,nofollow,noarchive";
    return () => {
      if (previous) meta.content = previousContent ?? "";
      else meta.remove();
    };
  }, []);

  useEffect(() => {
    void load("initial");
  }, [load]);

  const review = async (
    briefId: string,
    decision: "approve" | "reject",
  ) => {
    setReviewingId(briefId);
    setError(null);
    try {
      await marketMorningAdminApi.reviewEventBrief(
        briefId,
        decision,
        decision === "approve" ? "manual_quality_review" : "unsupported_claim",
      );
      const [nextSummary, nextQueue] = await Promise.all([
        marketMorningAdminApi.getSummary(24, 10),
        marketMorningAdminApi.listEventBriefs(filter, 20),
      ]);
      setSummary(nextSummary);
      setQueue(nextQueue);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "レビューを保存できませんでした。",
      );
    } finally {
      setReviewingId(null);
    }
  };

  const criticalCount = useMemo(
    () => summary?.alerts.filter((item) => item.severity === "critical").length ?? 0,
    [summary],
  );

  const reviewContentReport = async (
    reportId: string,
    decision: "resolve" | "dismiss",
  ) => {
    setReviewingReportId(reportId);
    setError(null);
    try {
      await marketMorningAdminApi.reviewContentReport(
        reportId,
        decision,
        decision === "resolve" ? "content_revised" : "no_issue_found",
      );
      setContentReports(
        await marketMorningAdminApi.listContentReports("pending", 50),
      );
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "ユーザー報告を更新できませんでした。",
      );
    } finally {
      setReviewingReportId(null);
    }
  };

  const reviewIssuerAlias = async (
    aliasId: string,
    decision: "approve" | "reject",
  ) => {
    setReviewingAliasId(aliasId);
    setError(null);
    try {
      await marketMorningAdminApi.reviewIssuerAlias(
        aliasId,
        decision,
        decision === "approve" ? "verified_company_name" : "ambiguous_alias",
      );
      setIssuerAliases(
        await marketMorningAdminApi.listIssuerAliases("pending", 50),
      );
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "銘柄別名レビューを保存できませんでした。",
      );
    } finally {
      setReviewingAliasId(null);
    }
  };

  const reviewMergeCandidate = async (
    candidateId: string,
    decision: "approve" | "reject",
  ) => {
    setReviewingCandidateId(candidateId);
    setError(null);
    try {
      await marketMorningAdminApi.reviewEventMergeCandidate(
        candidateId,
        decision,
        decision === "approve" ? "same_disclosure_event" : "distinct_events",
      );
      setMergeCandidates(
        await marketMorningAdminApi.listEventMergeCandidates("pending", 50),
      );
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "重複候補レビューを保存できませんでした。",
      );
    } finally {
      setReviewingCandidateId(null);
    }
  };

  const updateHalt = async (action: "create" | "revoke", editionDate: string) => {
    if (!editionDate) return;
    setHaltBusy(true);
    setError(null);
    try {
      if (action === "create") {
        await marketMorningAdminApi.createPublicationHalt(editionDate, haltReason);
      } else {
        await marketMorningAdminApi.revokePublicationHalt(editionDate);
      }
      setSummary(await marketMorningAdminApi.getSummary(24, 10));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "停発状態を更新できませんでした。");
    } finally {
      setHaltBusy(false);
    }
  };

  const createInvite = async () => {
    setInviteBusy(true);
    setError(null);
    setIssuedInvite(null);
    try {
      const result = await marketMorningAdminApi.createPrivateBetaInvite(inviteDays);
      setIssuedInvite({
        inviteId: result.invite_id,
        rawToken: result.raw_token,
        expiresAt: result.expires_at,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "招待を発行できませんでした。");
    } finally {
      setInviteBusy(false);
    }
  };

  const updateUserAccess = async (action: "suspend" | "reactivate") => {
    const userId = accessUserId.trim();
    if (!userId) return;
    setAccessBusy(true);
    setAccessNotice(null);
    setError(null);
    try {
      const result = await marketMorningAdminApi.setUserAccess(
        userId,
        action,
        action === "suspend" ? accessReason : "support_resolution",
      );
      setAccessNotice(
        result.account_status === "suspended"
          ? "ユーザー利用を停止しました。メール配信も無効です。"
          : "ユーザー利用を再開しました。メール配信は自動再開しません。",
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "ユーザー状態を更新できませんでした。");
    } finally {
      setAccessBusy(false);
    }
  };

  if (signedOut) {
    return (
      <div className="min-h-screen p-6 lg:p-8">
        <div className="mx-auto w-full max-w-3xl">
          <OperatorLoginState
            busy={authBusy}
            message={error}
            onLogin={() => void login()}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-6">
        <header className="flex flex-col gap-4 border-b pb-6 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-medium text-muted-foreground">
              <ShieldCheck className="h-3.5 w-3.5" />
              INTERNAL CONTROL PLANE
            </div>
            <h1 className="mt-3 text-3xl font-bold tracking-tight">Market Morning 運用</h1>
            <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
              直近24時間の生成・ソース・配信状態と、EventBrief の人手レビューを確認します。
              個人メモ、ユーザーID、モデル原文、ソースURLは表示しません。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => void load("refresh")}
              disabled={refreshing || authBusy}
              className="inline-flex items-center justify-center gap-2 rounded-md border px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
            >
              {refreshing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
              更新
            </button>
            {hasMarketMorningAuthLifecycleAdapter("operator") ? (
              <button
                type="button"
                onClick={() => void logout()}
                disabled={authBusy}
                className="inline-flex items-center justify-center gap-2 rounded-md border px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
              >
                <LogOut className="h-4 w-4" />
                ログアウト
              </button>
            ) : null}
          </div>
        </header>

        {loading ? <LoadingState /> : null}
        {!loading && authRequired && hasMarketMorningAuthLifecycleAdapter("operator") ? (
          <OperatorLoginState
            busy={authBusy}
            message={error}
            onLogin={() => void login()}
          />
        ) : null}
        {!loading && error && !authRequired ? <ErrorState message={error} /> : null}

        {!loading && !authRequired && summary ? (
          <>
            {preflight ? (
              <Panel title="デプロイ準備" icon={ShieldCheck}>
                <div
                  className={cn(
                    "rounded-md border p-4",
                    preflight.status === "configuration_ready"
                      ? "border-emerald-500/30 bg-emerald-500/5"
                      : "border-red-500/30 bg-red-500/5",
                  )}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="font-medium">
                      {preflight.status === "configuration_ready"
                        ? "静的設定 Gate は通過しています"
                        : "起動前に解消が必要な設定があります"}
                    </p>
                    <StatusBadge status={preflight.status} />
                  </div>
                  {preflight.blocking_checks.length ? (
                    <ul className="mt-3 space-y-2">
                      {preflight.blocking_checks.map((check) => (
                        <li key={check} className="flex flex-col gap-1 text-sm sm:flex-row sm:gap-3">
                          <code className="shrink-0 text-xs font-medium">{check}</code>
                          <span className="text-muted-foreground">
                            {DEPLOYMENT_CHECK_LABELS[check] ?? "設定を確認してください。"}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  <p className="mt-3 text-xs text-muted-foreground">
                    これは静的設定と DB の確認です。runtime provider と staging の検証を含まず、T1 の証拠にはなりません。
                  </p>
                </div>
              </Panel>
            ) : null}

            <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <MetricCard
                label="重大アラート"
                value={String(criticalCount)}
                icon={criticalCount ? XCircle : CheckCircle2}
                tone={criticalCount ? "danger" : "success"}
              />
              <MetricCard
                label="公式ソース"
                value={`${summary.sources.filter((item) => item.status === "healthy").length}/${summary.sources.length}`}
                icon={Database}
                tone={summary.sources.some((item) => item.status === "error") ? "danger" : "success"}
              />
              <MetricCard
                label="失敗 job"
                value={String(summary.job_counts.failed ?? 0)}
                icon={Activity}
                tone={(summary.job_counts.failed ?? 0) > 0 ? "danger" : "success"}
              />
              <MetricCard
                label="EventBrief 試行"
                value={String(summary.event_brief_generation_attempt_count)}
                icon={Clock3}
                tone="neutral"
              />
            </section>

            <section className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
              <Panel title="アラート" icon={AlertTriangle}>
                {summary.alerts.length ? (
                  <div className="space-y-2">
                    {summary.alerts.map((alert) => <AlertRow key={alert.code} alert={alert} />)}
                  </div>
                ) : (
                  <EmptyText>現在の閾値に該当するアラートはありません。</EmptyText>
                )}
              </Panel>
              <Panel title="コスト可観測性" icon={Activity}>
                <div
                  className={cn(
                    "rounded-md border p-4",
                    summary.cost_observability === "complete"
                      ? "border-emerald-500/30 bg-emerald-500/5"
                      : "border-amber-500/30 bg-amber-500/5",
                  )}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="font-medium">{COST_STATUS_LABELS[summary.cost_observability]}</p>
                    <p className="text-xs text-muted-foreground">
                      呼び出し {summary.model_usage.invocation_count}
                    </p>
                  </div>
                  <p className="mt-3 text-sm text-muted-foreground">
                    入力 {summary.model_usage.input_tokens.toLocaleString("ja-JP")} / 出力{" "}
                    {summary.model_usage.output_tokens.toLocaleString("ja-JP")} tokens
                  </p>
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
                    <span>Usage 欠損: {summary.model_usage.usage_missing_count}</span>
                    <span>未価格: {summary.model_usage.unpriced_count}</span>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {Object.entries(summary.model_usage.costs_by_currency).length ? (
                      Object.entries(summary.model_usage.costs_by_currency).map(([currency, micros]) => (
                        <span key={currency} className="rounded border bg-background px-2 py-1 text-xs font-medium">
                          {currency} {formatMicros(micros)}
                        </span>
                      ))
                    ) : (
                      <span className="text-xs text-muted-foreground">価格済みコストはありません。</span>
                    )}
                  </div>
                </div>
              </Panel>
            </section>

            <Panel title="刊行停止コントロール" icon={XCircle}>
              <p className="mb-4 text-sm text-muted-foreground">
                この操作は指定日の公開を停止するだけです。休市日や品質 Gate を上書きして公開する機能はありません。
              </p>
              <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end">
                <label className="grid gap-1.5 text-sm font-medium">
                  停発対象日
                  <input
                    type="date"
                    value={haltDate}
                    onChange={(event) => setHaltDate(event.target.value)}
                    className="rounded-md border bg-background px-3 py-2 font-normal"
                  />
                </label>
                <label className="grid gap-1.5 text-sm font-medium">
                  理由
                  <select
                    value={haltReason}
                    onChange={(event) => setHaltReason(event.target.value as PublicationHaltReason)}
                    className="rounded-md border bg-background px-3 py-2 font-normal"
                  >
                    <option value="content_quality_incident">コンテンツ品質</option>
                    <option value="source_incident">公式ソース障害</option>
                    <option value="market_data_incident">市場データ障害</option>
                    <option value="delivery_incident">配信障害</option>
                    <option value="operator_review">運用確認</option>
                  </select>
                </label>
                <button
                  type="button"
                  disabled={!haltDate || haltBusy}
                  onClick={() => void updateHalt("create", haltDate)}
                  className="rounded-md border border-red-500/30 px-4 py-2 text-sm font-medium text-red-700 hover:bg-red-500/10 disabled:opacity-50 dark:text-red-300"
                >
                  停発する
                </button>
              </div>
              {summary.active_halts.length ? (
                <div className="mt-4 divide-y rounded-md border">
                  {summary.active_halts.map((halt) => (
                    <div key={halt.edition_date} className="flex items-center justify-between gap-4 p-3">
                      <div>
                        <p className="font-medium">{halt.edition_date}</p>
                        <p className="mt-1 font-mono text-xs text-muted-foreground">{halt.reason_code}</p>
                      </div>
                      <button
                        type="button"
                        disabled={haltBusy}
                        onClick={() => void updateHalt("revoke", halt.edition_date)}
                        className="rounded-md border px-3 py-1.5 text-xs font-medium hover:bg-muted disabled:opacity-50"
                      >
                        停発を解除
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}
            </Panel>

            <section className="grid gap-4 xl:grid-cols-2">
              <Panel title="プライベートベータ招待" icon={UserPlus}>
                <p className="mb-4 text-sm text-muted-foreground">
                  招待シークレットは一度だけ表示され、データベースには SHA-256 のみ保存されます。
                </p>
                <div className="flex flex-wrap items-end gap-3">
                  <label className="grid gap-1.5 text-sm font-medium">
                    有効日数
                    <input
                      type="number"
                      min={1}
                      max={30}
                      value={inviteDays}
                      onChange={(event) => setInviteDays(Number(event.target.value))}
                      className="w-28 rounded-md border bg-background px-3 py-2 font-normal"
                    />
                  </label>
                  <button
                    type="button"
                    disabled={inviteBusy || inviteDays < 1 || inviteDays > 30}
                    onClick={() => void createInvite()}
                    className="rounded-md border px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
                  >
                    招待を発行
                  </button>
                </div>
                {issuedInvite ? (
                  <div className="mt-4 rounded-md border border-amber-500/30 bg-amber-500/5 p-4">
                    <p className="text-sm font-medium text-amber-700 dark:text-amber-300">
                      このトークンは再表示できません。安全な経路で共有してください。
                    </p>
                    <code className="mt-3 block break-all rounded bg-background p-3 text-xs">
                      {issuedInvite.rawToken}
                    </code>
                    <p className="mt-2 text-xs text-muted-foreground">
                      期限: {formatDateTime(issuedInvite.expiresAt)} · ID: {issuedInvite.inviteId}
                    </p>
                  </div>
                ) : null}
              </Panel>

              <Panel title="ユーザー利用制御" icon={UserX}>
                <p className="mb-4 text-sm text-muted-foreground">
                  停止すると製品アクセスとメール資格が直ちに失効します。削除処理とは別の操作です。
                </p>
                <label className="grid gap-1.5 text-sm font-medium">
                  ユーザーID
                  <input
                    value={accessUserId}
                    onChange={(event) => setAccessUserId(event.target.value)}
                    placeholder="UUID"
                    className="rounded-md border bg-background px-3 py-2 font-mono text-sm font-normal"
                  />
                </label>
                <label className="mt-3 grid gap-1.5 text-sm font-medium">
                  停止理由
                  <select
                    value={accessReason}
                    onChange={(event) => setAccessReason(event.target.value as UserAccessReason)}
                    className="rounded-md border bg-background px-3 py-2 font-normal"
                  >
                    <option value="beta_access_revoked">ベータ利用権の取消</option>
                    <option value="security_incident">セキュリティ対応</option>
                    <option value="user_request">ユーザー依頼</option>
                    <option value="support_resolution">サポート対応</option>
                  </select>
                </label>
                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={!accessUserId.trim() || accessBusy}
                    onClick={() => void updateUserAccess("suspend")}
                    className="rounded-md border border-red-500/30 px-4 py-2 text-sm font-medium text-red-700 hover:bg-red-500/10 disabled:opacity-50 dark:text-red-300"
                  >
                    利用を停止
                  </button>
                  <button
                    type="button"
                    disabled={!accessUserId.trim() || accessBusy}
                    onClick={() => void updateUserAccess("reactivate")}
                    className="rounded-md border px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
                  >
                    利用を再開
                  </button>
                </div>
                {accessNotice ? (
                  <p className="mt-3 text-sm text-muted-foreground">{accessNotice}</p>
                ) : null}
              </Panel>
            </section>

            <section className="grid gap-4 xl:grid-cols-2">
              <Panel title="公式ソース状態" icon={Database}>
                <div className="divide-y rounded-md border">
                  {summary.sources.map((source) => (
                    <div key={source.provider} className="flex items-start justify-between gap-4 p-3">
                      <div>
                        <p className="font-mono text-sm font-medium">{source.provider}</p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          最終成功: {formatDateTime(source.last_successful_discovery_at)}
                        </p>
                      </div>
                      <div className="text-right">
                        <StatusBadge status={source.status} />
                        {source.last_error_code ? (
                          <p className="mt-1 font-mono text-xs text-danger">{source.last_error_code}</p>
                        ) : null}
                      </div>
                    </div>
                  ))}
                </div>
              </Panel>
              <Panel title="最近の刊行 run" icon={Clock3}>
                {summary.global_runs.length ? (
                  <div className="space-y-2">
                    {summary.global_runs.map((run) => (
                      <div key={run.run_id} className="rounded-md border p-3">
                        <div className="flex items-center justify-between gap-3">
                          <p className="font-medium">{run.edition_date} / v{run.run_version}</p>
                          <StatusBadge status={run.status} />
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {run.attempt_key} · {run.scenario} · {run.is_current ? "current" : "history"}
                        </p>
                      </div>
                    ))}
                  </div>
                ) : <EmptyText>対象期間の run はありません。</EmptyText>}
              </Panel>
            </section>

            <Panel title="EventBrief レビュー" icon={ShieldCheck}>
              <div className="mb-4 flex flex-wrap gap-2">
                {REVIEW_FILTERS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setFilter(option.value)}
                    className={cn(
                      "rounded-md border px-3 py-1.5 text-xs font-medium",
                      filter === option.value ? "border-primary bg-primary/10 text-primary" : "text-muted-foreground hover:bg-muted",
                    )}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              {queue?.items.length ? (
                <div className="space-y-3">
                  {queue.items.map((item) => (
                    <article key={item.brief_id} className="rounded-md border p-4">
                      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                        <div>
                          <p className="text-xs font-medium text-muted-foreground">{item.issuer_code}</p>
                          <h3 className="mt-1 font-semibold">{item.legal_name_ja}</h3>
                          <p className="mt-2 text-sm">{item.event_title}</p>
                          <p className="mt-2 text-xs text-muted-foreground">
                            event v{item.event_version} · source {item.source_count} · {item.model_version} / {item.prompt_version}
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          {item.review_status === "auto_validated" ? (
                            <button
                              type="button"
                              aria-label="承認"
                              disabled={reviewingId === item.brief_id}
                              onClick={() => void review(item.brief_id, "approve")}
                              className="rounded-md border border-emerald-500/30 px-3 py-1.5 text-xs font-medium text-emerald-700 hover:bg-emerald-500/10 disabled:opacity-50 dark:text-emerald-300"
                            >
                              承認
                            </button>
                          ) : null}
                          {item.review_status !== "rejected" ? (
                            <button
                              type="button"
                              aria-label="却下"
                              disabled={reviewingId === item.brief_id}
                              onClick={() => void review(item.brief_id, "reject")}
                              className="rounded-md border border-red-500/30 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-500/10 disabled:opacity-50 dark:text-red-300"
                            >
                              却下
                            </button>
                          ) : null}
                        </div>
                      </div>
                    </article>
                  ))}
                </div>
              ) : <EmptyText>この状態の EventBrief はありません。</EmptyText>}
            </Panel>

            <section className="grid gap-4 xl:grid-cols-2">
              <Panel title="銘柄別名レビュー" icon={ShieldCheck}>
                <p className="mb-4 text-sm text-muted-foreground">
                  正式な銘柄名に紐づく検索用別名を確認します。参照元の内部情報や URL は表示しません。
                </p>
                {issuerAliases?.items.length ? (
                  <div className="space-y-3">
                    {issuerAliases.items.map((item) => (
                      <article key={item.alias_id} className="rounded-md border p-4">
                        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                          <div>
                            <p className="text-xs font-medium text-muted-foreground">
                              {item.issuer_code} · {item.source_type}
                            </p>
                            <h3 className="mt-1 font-semibold">{item.legal_name_ja}</h3>
                            <p className="mt-3 text-lg font-medium">{item.display_alias}</p>
                            <p className="mt-1 text-xs text-muted-foreground">
                              正規化: {item.normalized_alias} · 有効開始: {item.effective_from}
                            </p>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            <button
                              type="button"
                              aria-label="別名を承認"
                              disabled={reviewingAliasId === item.alias_id}
                              onClick={() => void reviewIssuerAlias(item.alias_id, "approve")}
                              className="rounded-md border border-emerald-500/30 px-3 py-1.5 text-xs font-medium text-emerald-700 hover:bg-emerald-500/10 disabled:opacity-50 dark:text-emerald-300"
                            >
                              承認
                            </button>
                            <button
                              type="button"
                              aria-label="別名を却下"
                              disabled={reviewingAliasId === item.alias_id}
                              onClick={() => void reviewIssuerAlias(item.alias_id, "reject")}
                              className="rounded-md border border-red-500/30 px-3 py-1.5 text-xs font-medium text-red-700 hover:bg-red-500/10 disabled:opacity-50 dark:text-red-300"
                            >
                              却下
                            </button>
                          </div>
                        </div>
                      </article>
                    ))}
                  </div>
                ) : (
                  <EmptyText>未対応の銘柄別名はありません。</EmptyText>
                )}
              </Panel>

              <Panel title="跨ソース重複候補" icon={Activity}>
                <p className="mb-4 text-sm text-muted-foreground">
                  承認してもイベントは自動統合されません。候補の審査結果だけを記録し、既存イベントと公開済み朝刊は変更しません。
                </p>
                {mergeCandidates?.items.length ? (
                  <div className="space-y-3">
                    {mergeCandidates.items.map((item) => (
                      <article key={item.candidate_id} className="rounded-md border p-4">
                        <div className="flex flex-col gap-3">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div>
                              <p className="text-xs font-medium text-muted-foreground">
                                {item.issuer_code}
                              </p>
                              <h3 className="mt-1 font-semibold">{item.legal_name_ja}</h3>
                            </div>
                            <span className="rounded border px-2 py-1 text-xs font-medium">
                              類似度 {Math.round(item.title_similarity * 100)}% · {item.time_distance_seconds}秒差
                            </span>
                          </div>
                          <div className="grid gap-2 sm:grid-cols-2">
                            <div className="rounded-md border bg-muted/20 p-3">
                              <p className="text-[11px] font-medium uppercase text-muted-foreground">
                                Event A · {item.left_event_type}
                              </p>
                              <p className="mt-2 text-sm">{item.left_event_title}</p>
                            </div>
                            <div className="rounded-md border bg-muted/20 p-3">
                              <p className="text-[11px] font-medium uppercase text-muted-foreground">
                                Event B · {item.right_event_type}
                              </p>
                              <p className="mt-2 text-sm">{item.right_event_title}</p>
                            </div>
                          </div>
                          <div className="flex flex-wrap justify-end gap-2">
                            <button
                              type="button"
                              aria-label="同一イベントとして承認"
                              disabled={reviewingCandidateId === item.candidate_id}
                              onClick={() => void reviewMergeCandidate(item.candidate_id, "approve")}
                              className="rounded-md border border-emerald-500/30 px-3 py-1.5 text-xs font-medium text-emerald-700 hover:bg-emerald-500/10 disabled:opacity-50 dark:text-emerald-300"
                            >
                              同一イベント
                            </button>
                            <button
                              type="button"
                              aria-label="別イベント"
                              disabled={reviewingCandidateId === item.candidate_id}
                              onClick={() => void reviewMergeCandidate(item.candidate_id, "reject")}
                              className="rounded-md border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted disabled:opacity-50"
                            >
                              別イベント
                            </button>
                          </div>
                        </div>
                      </article>
                    ))}
                  </div>
                ) : (
                  <EmptyText>未対応の跨ソース重複候補はありません。</EmptyText>
                )}
              </Panel>
            </section>

            <Panel title="ユーザー報告" icon={Flag}>
              <p className="mb-4 text-sm text-muted-foreground">
                朝刊の事実または出典に対する固定理由の報告です。ユーザーID、自由記述、ソースURLは表示しません。
              </p>
              {contentReports?.items.length ? (
                <div className="space-y-3">
                  {contentReports.items.map((item) => (
                    <article key={item.report_id} className="rounded-md border p-4">
                      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                        <div>
                          <p className="text-xs font-medium text-muted-foreground">
                            {item.edition_date} · {item.issuer_code} · {item.event_type}
                          </p>
                          <h3 className="mt-1 font-semibold">{item.legal_name_ja}</h3>
                          <p className="mt-2 text-sm">{item.event_title}</p>
                          <p className="mt-2 text-xs font-medium text-amber-700 dark:text-amber-300">
                            {CONTENT_REPORT_REASON_LABELS[item.reason_code] ?? item.reason_code}
                          </p>
                        </div>
                        <div className="flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            disabled={reviewingReportId === item.report_id}
                            onClick={() => void reviewContentReport(item.report_id, "resolve")}
                            className="rounded-md border border-emerald-500/30 px-3 py-1.5 text-xs font-medium text-emerald-700 hover:bg-emerald-500/10 disabled:opacity-50 dark:text-emerald-300"
                          >
                            内容を修正済み
                          </button>
                          <button
                            type="button"
                            disabled={reviewingReportId === item.report_id}
                            onClick={() => void reviewContentReport(item.report_id, "dismiss")}
                            className="rounded-md border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted disabled:opacity-50"
                          >
                            問題なし
                          </button>
                        </div>
                      </div>
                    </article>
                  ))}
                </div>
              ) : (
                <EmptyText>未対応のユーザー報告はありません。</EmptyText>
              )}
            </Panel>
          </>
        ) : null}
      </div>
    </div>
  );
}

function OperatorAuthBoundary() {
  const configured = hasMarketMorningAuthLifecycleAdapter("operator");
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState<"loading" | "ready" | "error">(
    configured ? "loading" : "ready",
  );

  useEffect(() => {
    if (!configured) {
      setStatus("ready");
      return;
    }
    let active = true;
    setStatus("loading");
    void initializeMarketMorningAuth("operator")
      .then(() => {
        if (active) setStatus("ready");
      })
      .catch(() => {
        if (active) setStatus("error");
      });
    return () => {
      active = false;
    };
  }, [attempt, configured]);

  if (status === "loading") {
    return <div className="min-h-screen p-6 lg:p-8"><LoadingState /></div>;
  }
  if (status === "error") {
    return (
      <div className="min-h-screen p-6 lg:p-8">
        <div className="mx-auto w-full max-w-3xl">
          <ErrorState message="運用ログイン状態を初期化できませんでした。" />
          <button
            type="button"
            onClick={() => setAttempt((value) => value + 1)}
            className="mt-4 inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium hover:bg-muted"
          >
            <RefreshCw className="h-4 w-4" />
            認証を再試行
          </button>
        </div>
      </div>
    );
  }
  return <MarketMorningOperationsContent />;
}

export function MarketMorningOperations() {
  return <OperatorAuthBoundary />;
}

function LoadingState() {
  return <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{[1, 2, 3, 4].map((item) => <div key={item} className="h-24 animate-pulse rounded-md border bg-muted/40" />)}</div>;
}

function ErrorState({ message }: { message: string }) {
  return <section className="rounded-md border border-red-500/30 bg-red-500/5 p-5"><p className="font-medium text-red-700 dark:text-red-300">運用データを取得できません</p><p className="mt-2 text-sm text-muted-foreground">{message}</p></section>;
}

function OperatorLoginState({ busy, message, onLogin }: { busy: boolean; message: string | null; onLogin: () => void }) {
  return (
    <section className="rounded-md border border-amber-500/30 bg-amber-500/5 p-5">
      <p className="font-medium">運用ログインが必要です</p>
      <p className="mt-2 text-sm text-muted-foreground">
        個人を識別できる運用アカウントでログインしてください。
      </p>
      {message ? <p className="mt-2 text-sm text-red-700 dark:text-red-300">{message}</p> : null}
      <button
        type="button"
        onClick={onLogin}
        disabled={busy}
        className="mt-4 inline-flex items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
      >
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <LogIn className="h-4 w-4" />}
        運用アカウントでログイン
      </button>
    </section>
  );
}

function MetricCard({ label, value, icon: Icon, tone }: { label: string; value: string; icon: typeof Activity; tone: "success" | "danger" | "neutral" }) {
  return <div className="rounded-md border p-4"><div className="flex items-center justify-between"><span className="text-xs font-medium uppercase text-muted-foreground">{label}</span><Icon className={cn("h-4 w-4", tone === "success" && "text-emerald-500", tone === "danger" && "text-red-500", tone === "neutral" && "text-muted-foreground")} /></div><p className="mt-3 text-2xl font-semibold">{value}</p></div>;
}

function Panel({ title, icon: Icon, children }: { title: string; icon: typeof Activity; children: React.ReactNode }) {
  return <section className="rounded-md border p-5"><h2 className="flex items-center gap-2 font-semibold"><Icon className="h-4 w-4 text-muted-foreground" />{title}</h2><div className="mt-4">{children}</div></section>;
}

function AlertRow({ alert }: { alert: OperationsAlert }) {
  return <div className={cn("flex items-center justify-between rounded-md border p-3", alert.severity === "critical" ? "border-red-500/30 bg-red-500/5" : "border-amber-500/30 bg-amber-500/5")}><span className="text-sm font-medium">{ALERT_LABELS[alert.code] ?? alert.code}</span><span className="font-mono text-xs">{alert.count}</span></div>;
}

function StatusBadge({ status }: { status: string }) {
  const good = ["healthy", "complete", "partial", "late", "published", "approved"].includes(status);
  const bad = ["error", "failed", "blocked", "rejected"].includes(status);
  return <span className={cn("inline-flex rounded-full border px-2 py-0.5 text-[11px] font-medium", good && "border-emerald-500/30 text-emerald-700 dark:text-emerald-300", bad && "border-red-500/30 text-red-700 dark:text-red-300", !good && !bad && "text-muted-foreground")}>{status}</span>;
}

function EmptyText({ children }: { children: React.ReactNode }) {
  return <p className="rounded-md border border-dashed p-5 text-center text-sm text-muted-foreground">{children}</p>;
}

function formatDateTime(value: string | null): string {
  if (!value) return "未記録";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "不明";
  return new Intl.DateTimeFormat("ja-JP", { dateStyle: "short", timeStyle: "short", timeZone: "Asia/Tokyo" }).format(parsed);
}
