import {
  AlertTriangle,
  ArrowRight,
  CalendarClock,
  Check,
  ExternalLink,
  FileText,
  Search,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  marketMorningApi,
  type EditionEventState,
  type EditionFact,
  type EditionIssuer,
  type EditionSourceCitation,
  type ContentReportReason,
  type MorningEdition,
  type TodayEditionResponse,
} from "@/lib/marketMorningApi";
import { useMarketMorning } from "@/pages/market-morning/MarketMorningContext";

const ICON_STROKE = 1.7;

type EditionState = "no_data" | "market_holiday" | "not_published" | "partial";

const EDITION_COPY: Record<EditionState, { title: string; body: string }> = {
  no_data: {
    title: "確認できるデータがありません",
    body: "データ取得の状態を確認中です。情報がないことと、重要な変化がないことは区別して表示します。",
  },
  market_holiday: {
    title: "本日は東京市場の休場日です",
    body: "通常の開場前朝刊は発行しません。次の取引日に必要な確認事項だけを整理します。",
  },
  not_published: {
    title: "本日の朝刊はまだ公開されていません",
    body: "公式情報の収集と出典確認が完了してから公開します。未公開を「重要な変化なし」とは表示しません。",
  },
  partial: {
    title: "一部の情報だけ確認できています",
    body: "取得できた情報を先に表示し、未確認の銘柄と情報源を明示します。",
  },
};

export function EditionStatusPanel({ state }: { state: EditionState }) {
  const copy = EDITION_COPY[state];
  return (
    <section className="border-t-2 border-[#18263a] bg-[#fffdf8] p-6 dark:border-slate-500 dark:bg-slate-900 md:p-8" data-edition-state={state}>
      <CalendarClock strokeWidth={ICON_STROKE} className="h-7 w-7 text-[#1f6090] dark:text-sky-400" />
      <h2 className="market-morning-serif mt-5 text-2xl font-semibold tracking-[-0.035em] text-[#18263a] dark:text-slate-100">
        {copy.title}
      </h2>
      <p className="mt-3 max-w-[62ch] text-sm leading-7 text-[#5e6f80] dark:text-slate-300">
        {copy.body}
      </p>
    </section>
  );
}

function providerLabel(provider: string) {
  if (provider === "fixture_tdnet") return "TDnet（デモ）";
  if (provider === "fixture_edinet") return "EDINET（デモ）";
  if (provider.startsWith("fixture_company_ir")) return "会社IR（デモ）";
  return provider.startsWith("fixture_") ? "一次情報（デモ）" : provider;
}

function lifecycleLabel(status: EditionFact["lifecycle_status"]) {
  if (status === "corrected") return "訂正";
  if (status === "withdrawn") return "撤回";
  return null;
}

function formatPublishedAt(value: string) {
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

interface EventFactGroup {
  eventId: string;
  facts: EditionFact[];
  citations: EditionSourceCitation[];
}

function groupEventFacts(facts: EditionFact[]): EventFactGroup[] {
  const groups = new Map<string, EventFactGroup>();
  for (const fact of facts) {
    const group = groups.get(fact.event_id) ?? {
      eventId: fact.event_id,
      facts: [],
      citations: [],
    };
    group.facts.push(fact);
    const citationKeys = new Set(
      group.citations.map(
        (item) => `${item.provider}\u0000${item.document_id}\u0000${item.revision_key}`,
      ),
    );
    for (const citation of fact.citations) {
      const key = `${citation.provider}\u0000${citation.document_id}\u0000${citation.revision_key}`;
      if (!citationKeys.has(key)) {
        group.citations.push(citation);
        citationKeys.add(key);
      }
    }
    groups.set(fact.event_id, group);
  }
  return [...groups.values()];
}

const EVENT_STATE_OPTIONS: ReadonlyArray<{
  state: EditionEventState;
  label: string;
}> = [
  { state: "read", label: "既読" },
  { state: "later", label: "あとで" },
  { state: "irrelevant", label: "関係なし" },
];

const CONTENT_REPORT_REASONS: ReadonlyArray<{
  value: ContentReportReason;
  label: string;
}> = [
  { value: "fact_inaccurate", label: "事実が正確ではない" },
  { value: "source_mismatch", label: "出典と内容が一致しない" },
  { value: "outdated_or_corrected", label: "古い情報・訂正前の内容" },
  { value: "other_content_issue", label: "その他の内容上の問題" },
];

function EventFactRow({
  group,
  state,
  interactionDisabled,
  reportEnabled,
  reportPending,
  reported,
  onStateChange,
  onSourceOpen,
  onReport,
}: {
  group: EventFactGroup;
  state?: EditionEventState;
  interactionDisabled: boolean;
  reportEnabled: boolean;
  reportPending: boolean;
  reported: boolean;
  onStateChange?: (eventId: string, state: EditionEventState) => void;
  onSourceOpen?: (eventId: string, citation: EditionSourceCitation) => void;
  onReport?: (eventId: string, reason: ContentReportReason) => void;
}) {
  const [reportOpen, setReportOpen] = useState(false);
  const [reportReason, setReportReason] = useState<ContentReportReason>(
    "fact_inaccurate",
  );
  const lifecycle = group.facts
    .map((fact) => lifecycleLabel(fact.lifecycle_status))
    .find((label) => label !== null);
  return (
    <li className="border-t border-[#d9d4c8] py-5 first:border-t-0 first:pt-0 dark:border-slate-700">
      <div className="flex flex-wrap items-start gap-2">
        {lifecycle ? (
          <span className="border border-[#b8533c] bg-[#fff0e8] px-2 py-0.5 text-[10px] font-semibold text-[#8f3828] dark:border-orange-600 dark:bg-orange-950 dark:text-orange-300">
            {lifecycle}
          </span>
        ) : null}
        {group.facts.length === 1 ? (
          <p className="min-w-0 flex-1 text-sm font-semibold leading-6 text-[#24384a] dark:text-slate-100">
            {group.facts[0].text}
          </p>
        ) : (
          <ul className="min-w-0 flex-1 list-disc space-y-1 pl-5 text-sm font-semibold leading-6 text-[#24384a] dark:text-slate-100">
            {group.facts.map((fact, index) => (
              <li key={`${fact.event_id}-${index}-${fact.text}`}>{fact.text}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2">
        {group.citations.map((citation) => (
          <a
            key={`${citation.provider}-${citation.document_id}-${citation.revision_key}`}
            href={citation.original_url}
            target="_blank"
            rel="noreferrer noopener"
            onClick={() => onSourceOpen?.(group.eventId, citation)}
            className="inline-flex items-center gap-1.5 text-xs font-semibold text-[#1f6090] hover:underline dark:text-sky-400"
          >
            <FileText strokeWidth={ICON_STROKE} className="h-3.5 w-3.5" />
            {providerLabel(citation.provider)}
            <span className="font-normal text-[#72808c] dark:text-slate-500">
              {formatPublishedAt(citation.published_at)}
            </span>
            <ExternalLink strokeWidth={ICON_STROKE} className="h-3 w-3" />
          </a>
        ))}
      </div>
      {onStateChange ? (
        <div className="mt-4 flex flex-wrap items-center gap-1.5" aria-label="この記事の整理">
          {EVENT_STATE_OPTIONS.map((option) => {
            const selected = state === option.state;
            return (
              <button
                key={option.state}
                type="button"
                aria-pressed={selected}
                disabled={interactionDisabled}
                onClick={() => onStateChange(group.eventId, option.state)}
                className={`border px-2.5 py-1 text-[11px] font-semibold transition disabled:cursor-wait disabled:opacity-60 ${selected ? "border-[#1f6090] bg-[#1f6090] text-white dark:border-sky-500 dark:bg-sky-600" : "border-[#c6c0b4] text-[#64717b] hover:border-[#1f6090] hover:text-[#1f6090] dark:border-slate-700 dark:text-slate-400"}`}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      ) : null}
      {reportEnabled ? (
        <div className="mt-3 border-t border-dashed border-[#d9d4c8] pt-3 dark:border-slate-700">
          {reported ? (
            <span className="text-[11px] font-semibold text-[#15766d] dark:text-emerald-400">
              報告済み
            </span>
          ) : reportOpen ? (
            <div className="flex flex-wrap items-end gap-2">
              <label className="grid gap-1 text-[11px] font-semibold text-[#64717b] dark:text-slate-400">
                報告理由
                <select
                  aria-label="報告理由"
                  value={reportReason}
                  disabled={reportPending}
                  onChange={(event) =>
                    setReportReason(event.target.value as ContentReportReason)
                  }
                  className="border border-[#c6c0b4] bg-transparent px-2.5 py-1.5 text-xs font-normal text-[#24384a] dark:border-slate-700 dark:text-slate-200"
                >
                  {CONTENT_REPORT_REASONS.map((reason) => (
                    <option key={reason.value} value={reason.value}>
                      {reason.label}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                disabled={reportPending}
                onClick={() => onReport?.(group.eventId, reportReason)}
                className="border border-[#1f6090] px-2.5 py-1.5 text-[11px] font-semibold text-[#1f6090] hover:bg-[#e8f0f2] disabled:cursor-wait disabled:opacity-60 dark:border-sky-500 dark:text-sky-400 dark:hover:bg-slate-800"
              >
                報告を送信
              </button>
              <button
                type="button"
                disabled={reportPending}
                onClick={() => setReportOpen(false)}
                className="px-2.5 py-1.5 text-[11px] font-semibold text-[#72808c] disabled:opacity-60 dark:text-slate-500"
              >
                キャンセル
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setReportOpen(true)}
              className="text-[11px] font-semibold text-[#72808c] underline-offset-2 hover:text-[#8f3828] hover:underline dark:text-slate-500 dark:hover:text-orange-300"
            >
              内容を報告
            </button>
          )}
        </div>
      ) : null}
    </li>
  );
}

function IssuerBriefCard({
  issuer,
  order,
  eventStates,
  pendingEventId,
  pendingReportEventId,
  reportedEventIds,
  reportEnabled,
  onStateChange,
  onSourceOpen,
  onReport,
}: {
  issuer: EditionIssuer;
  order: number;
  eventStates: Record<string, EditionEventState>;
  pendingEventId: string | null;
  pendingReportEventId: string | null;
  reportedEventIds: ReadonlySet<string>;
  reportEnabled: boolean;
  onStateChange?: (eventId: string, state: EditionEventState) => void;
  onSourceOpen?: (eventId: string, citation: EditionSourceCitation) => void;
  onReport?: (eventId: string, reason: ContentReportReason) => void;
}) {
  const unavailable = issuer.status === "unavailable";
  const noEvents = issuer.status === "no_confirmed_events";
  return (
    <article className="grid border-t-2 border-[#18263a] bg-[#fffdf8] dark:border-slate-500 dark:bg-slate-900 md:grid-cols-[170px_minmax(0,1fr)]">
      <header className="border-b border-[#d9d4c8] p-5 dark:border-slate-700 md:border-b-0 md:border-r md:p-6">
        <p className="font-mono text-[10px] tracking-[0.08em] text-[#7a8793] dark:text-slate-500">
          {String(order).padStart(2, "0")} / {issuer.issuer_code}
        </p>
        <h3 className="market-morning-serif mt-3 text-xl font-semibold leading-7 tracking-[-0.035em] text-[#18263a] dark:text-slate-100">
          {issuer.legal_name_ja}
        </h3>
      </header>

      <div className="p-5 md:p-6">
        {unavailable ? (
          <div className="flex gap-3 border border-[#d6b8a6] bg-[#fff6ee] p-4 dark:border-orange-900 dark:bg-orange-950/40">
            <AlertTriangle strokeWidth={ICON_STROKE} className="mt-0.5 h-5 w-5 shrink-0 text-[#a64b31] dark:text-orange-400" />
            <div>
              <strong className="text-sm text-[#753a2b] dark:text-orange-200">データ取得不可</strong>
              <p className="mt-1 text-xs leading-6 text-[#785d50] dark:text-orange-300/80">
                この銘柄の情報源を確認できませんでした。情報なしとは判定していません。
              </p>
            </div>
          </div>
        ) : noEvents ? (
          <div className="border border-[#c8d5d8] bg-[#f0f5f4] p-4 dark:border-slate-700 dark:bg-slate-800">
            <strong className="text-sm text-[#365664] dark:text-slate-200">確認済みの新規情報なし</strong>
            <p className="mt-1 text-xs leading-6 text-[#617682] dark:text-slate-400">
              対象期間の一次情報を確認しましたが、新しい開示は見つかりませんでした。
            </p>
          </div>
        ) : (
          <section aria-label={`${issuer.legal_name_ja}の確認できた事実`}>
            <p className="font-mono text-[10px] tracking-[0.08em] text-[#15766d] dark:text-emerald-400">
              確認できた事実
            </p>
            <ul className="mt-4">
              {groupEventFacts(issuer.facts).map((group) => (
                <EventFactRow
                  key={group.eventId}
                  group={group}
                  state={eventStates[group.eventId]}
                  interactionDisabled={pendingEventId === group.eventId}
                  reportEnabled={reportEnabled}
                  reportPending={pendingReportEventId === group.eventId}
                  reported={reportedEventIds.has(group.eventId)}
                  onStateChange={onStateChange}
                  onSourceOpen={onSourceOpen}
                  onReport={onReport}
                />
              ))}
            </ul>
          </section>
        )}

        <section className="mt-5 border-l-2 border-[#c8c1b4] pl-4 dark:border-slate-600" aria-label="判断">
          <p className="font-mono text-[10px] tracking-[0.08em] text-[#7a756c] dark:text-slate-500">判断</p>
          <p className="mt-1 text-sm font-semibold text-[#474c50] dark:text-slate-300">
            {issuer.assessment.text}
          </p>
          <p className="mt-1 text-xs leading-5 text-[#77766f] dark:text-slate-500">
            確認済みの事実だけでは、値動きや売買判断を導けません。
          </p>
        </section>
      </div>
    </article>
  );
}

function MorningEditionView({
  edition,
  editionId,
  dataMode,
}: {
  edition: MorningEdition;
  editionId: string | null;
  dataMode: TodayEditionResponse["data_mode"];
}) {
  const [eventStates, setEventStates] = useState<Record<string, EditionEventState>>({});
  const [pendingEventId, setPendingEventId] = useState<string | null>(null);
  const [pendingReportEventId, setPendingReportEventId] = useState<string | null>(null);
  const [reportedEventIds, setReportedEventIds] = useState<Set<string>>(new Set());
  const [interactionError, setInteractionError] = useState<string | null>(null);

  useEffect(() => {
    setEventStates({});
    setReportedEventIds(new Set());
    setInteractionError(null);
    if (!editionId) return;
    const controller = new AbortController();
    marketMorningApi.getEditionEventStates(editionId, controller.signal).then(
      ({ items }) => {
        setEventStates(
          Object.fromEntries(items.map((item) => [item.event_id, item.state])),
        );
      },
      (error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setInteractionError("記事の整理状態を読み込めませんでした。");
      },
    );
    return () => controller.abort();
  }, [editionId]);

  const updateEventState = async (eventId: string, state: EditionEventState) => {
    if (!editionId) return;
    const previous = eventStates[eventId];
    setEventStates((current) => ({ ...current, [eventId]: state }));
    setPendingEventId(eventId);
    setInteractionError(null);
    try {
      const result = await marketMorningApi.updateEditionEventState(
        editionId,
        eventId,
        state,
      );
      setEventStates((current) => ({ ...current, [eventId]: result.state }));
    } catch {
      setEventStates((current) => {
        const next = { ...current };
        if (previous) next[eventId] = previous;
        else delete next[eventId];
        return next;
      });
      setInteractionError("記事の整理状態を保存できませんでした。");
    } finally {
      setPendingEventId(null);
    }
  };

  const recordSourceOpen = (
    eventId: string,
    citation: EditionSourceCitation,
  ) => {
    if (!editionId) return;
    void marketMorningApi
      .recordEditionSourceOpen(editionId, {
        event_id: eventId,
        provider: citation.provider,
        document_id: citation.document_id,
        revision_key: citation.revision_key,
        request_id: globalThis.crypto.randomUUID(),
      })
      .catch(() => {
        setInteractionError("出典の閲覧記録を保存できませんでした。");
      });
  };

  const reportEvent = async (
    eventId: string,
    reason: ContentReportReason,
  ) => {
    if (!editionId || dataMode !== "persisted") return;
    setPendingReportEventId(eventId);
    setInteractionError(null);
    try {
      await marketMorningApi.reportEditionEvent(editionId, eventId, reason);
      setReportedEventIds((current) => new Set(current).add(eventId));
    } catch {
      setInteractionError("内容の報告を送信できませんでした。");
    } finally {
      setPendingReportEventId(null);
    }
  };

  return (
    <div className="space-y-5">
      {dataMode === "synthetic_fixture" ? (
        <section className="grid gap-3 border-2 border-[#b85c36] bg-[#fff1e7] p-4 text-[#713724] dark:border-orange-600 dark:bg-orange-950/50 dark:text-orange-100 md:grid-cols-[auto_minmax(0,1fr)] md:items-center">
          <span className="inline-flex w-fit items-center gap-2 bg-[#a84729] px-3 py-1.5 text-xs font-bold tracking-[0.08em] text-white dark:bg-orange-600">
            <Sparkles strokeWidth={ICON_STROKE} className="h-4 w-4" />
            デモデータ
          </span>
          <p className="text-xs font-medium leading-6">
            画面確認用に生成した架空の情報です。実在の市場情報・投資判断には使用できません。
          </p>
        </section>
      ) : null}

      {edition.status === "partial" ? (
        <section className="flex gap-3 border border-[#d5b39e] bg-[#fff8f0] p-4 dark:border-orange-900 dark:bg-orange-950/30">
          <AlertTriangle strokeWidth={ICON_STROKE} className="mt-0.5 h-5 w-5 shrink-0 text-[#a64b31] dark:text-orange-400" />
          <div>
            <h2 className="text-sm font-semibold text-[#753a2b] dark:text-orange-200">一部の情報源を確認できません</h2>
            <p className="mt-1 text-xs leading-6 text-[#785d50] dark:text-orange-300/80">
              確認できた事実だけを掲載しています。取得できなかった銘柄は個別に明示します。
            </p>
          </div>
        </section>
      ) : null}

      {interactionError ? (
        <p className="border-l-2 border-[#a64b31] pl-3 text-xs text-[#8f3828] dark:border-orange-500 dark:text-orange-300" role="status">
          {interactionError}
        </p>
      ) : null}

      <div className="grid gap-5">
        {edition.issuers.map((issuer, index) => (
          <IssuerBriefCard
            key={issuer.issuer_id}
            issuer={issuer}
            order={index + 1}
            eventStates={eventStates}
            pendingEventId={pendingEventId}
            pendingReportEventId={pendingReportEventId}
            reportedEventIds={reportedEventIds}
            reportEnabled={dataMode === "persisted" && Boolean(editionId)}
            onStateChange={editionId ? updateEventState : undefined}
            onSourceOpen={editionId ? recordSourceOpen : undefined}
            onReport={editionId ? reportEvent : undefined}
          />
        ))}
      </div>

      <footer className="flex flex-wrap justify-between gap-3 border-t border-[#cfc9bc] pt-4 text-[10px] leading-5 text-[#737870] dark:border-slate-800 dark:text-slate-500">
        <span>生成日時 {formatPublishedAt(edition.generated_at)} JST</span>
        <span>確認イベント {edition.consumed_event_units} 件</span>
      </footer>
    </div>
  );
}

function Onboarding({ activeCount }: { activeCount: number }) {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1.35fr)_minmax(280px,0.65fr)]">
      <section className="border-t-2 border-[#18263a] bg-[#fffdf8] p-6 dark:border-slate-500 dark:bg-slate-900 md:p-8">
        <Search strokeWidth={ICON_STROKE} className="h-7 w-7 text-[#1f6090] dark:text-sky-400" />
        <h2 className="market-morning-serif mt-5 text-2xl font-semibold tracking-[-0.035em] text-[#18263a] dark:text-slate-100 md:text-3xl">
          まず、3銘柄を選びます。
        </h2>
        <p className="mt-3 max-w-[58ch] text-sm leading-7 text-[#5e6f80] dark:text-slate-300">
          毎朝確認したい会社を3-10銘柄に絞ります。売買候補ではなく、一次情報を継続して読むための注目リストです。
        </p>
        <Link
          to="/market-morning/app/watchlist"
          className="mt-7 inline-flex items-center gap-2 whitespace-nowrap bg-[#1f6090] px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-[#184f78] active:translate-y-px dark:bg-sky-600 dark:hover:bg-sky-500"
        >
          銘柄を検索する
          <ArrowRight strokeWidth={ICON_STROKE} className="h-4 w-4" />
        </Link>
      </section>

      <aside className="bg-[#e8f0f2] p-6 dark:bg-slate-900">
        <p className="font-mono text-[10px] tracking-[0.08em] text-[#1f6090] dark:text-sky-400">
          ONBOARDING
        </p>
        <p className="mt-3 text-4xl font-semibold tracking-[-0.05em] text-[#18263a] dark:text-slate-100">
          {activeCount}<span className="ml-1 text-base font-medium text-[#667789] dark:text-slate-400">/ 3</span>
        </p>
        <div className="mt-5 grid grid-cols-3 gap-2" aria-label={`${activeCount} of 3 selected`}>
          {[0, 1, 2].map((index) => (
            <span
              key={index}
              className={`grid h-10 place-items-center border ${index < activeCount ? "border-[#15766d] bg-[#e2f0ec] text-[#15766d] dark:border-emerald-500 dark:bg-emerald-950 dark:text-emerald-300" : "border-[#c9c4b8] text-[#9a988f] dark:border-slate-700 dark:text-slate-600"}`}
            >
              {index < activeCount ? <Check strokeWidth={ICON_STROKE} className="h-4 w-4" /> : index + 1}
            </span>
          ))}
        </div>
        <p className="mt-5 text-xs leading-6 text-[#5e6f80] dark:text-slate-400">
          3銘柄を登録すると、朝刊の準備状態をこの画面で確認できます。
        </p>
      </aside>
    </div>
  );
}

export function MarketMorningToday() {
  const { watchlist } = useMarketMorning();
  const activeCount = watchlist?.active_count ?? 0;
  const [editionResponse, setEditionResponse] = useState<TodayEditionResponse | null>(null);
  const [editionLoading, setEditionLoading] = useState(false);
  const [editionFailed, setEditionFailed] = useState(false);

  useEffect(() => {
    if (activeCount < 3) {
      setEditionResponse(null);
      setEditionLoading(false);
      setEditionFailed(false);
      return;
    }

    const controller = new AbortController();
    setEditionLoading(true);
    setEditionFailed(false);
    marketMorningApi.getTodayEdition(controller.signal).then(
      (response) => {
        setEditionResponse(response);
        setEditionLoading(false);
      },
      (error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setEditionResponse(null);
        setEditionFailed(true);
        setEditionLoading(false);
      },
    );
    return () => controller.abort();
  }, [activeCount]);

  const dateLabel = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  }).format(new Date());

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-8 md:px-8 md:py-10">
      <header className="grid gap-6 border-b border-[#d9d4c8] pb-8 dark:border-slate-800 md:grid-cols-[minmax(0,1fr)_300px] md:items-end">
        <div>
          <p className="font-mono text-[10px] tracking-[0.08em] text-[#1f6090] dark:text-sky-400">{dateLabel} JST</p>
          <h1 className="market-morning-serif mt-3 max-w-3xl text-4xl font-semibold leading-[1.25] tracking-[-0.055em] text-[#18263a] dark:text-slate-100 md:text-5xl">
            市場のノイズを、<br />自分の論点に戻す。
          </h1>
          <p className="mt-4 max-w-[62ch] text-sm leading-7 text-[#5e6f80] dark:text-slate-300">
            米国市場から日本の寄り付きまで。注目銘柄に関係する確認済みの事実を、出典と一緒に整理します。
          </p>
        </div>
        <aside className="border-l-2 border-[#1f6090] bg-[#e8f0f2] p-4 dark:border-sky-500 dark:bg-slate-900">
          <ShieldCheck strokeWidth={ICON_STROKE} className="h-5 w-5 text-[#1f6090] dark:text-sky-400" />
          <strong className="mt-3 block text-sm text-[#24384a] dark:text-slate-100">朝刊の読み方</strong>
          <p className="mt-2 text-xs leading-6 text-[#586e80] dark:text-slate-400">
            値動きの予想ではなく、今日確認する事実と未確認事項から始めます。
          </p>
        </aside>
      </header>

      <div className="mt-8">
        {activeCount < 3 ? <Onboarding activeCount={activeCount} /> : null}
        {activeCount >= 3 && editionLoading ? (
          <section className="border-t-2 border-[#18263a] bg-[#fffdf8] p-6 dark:border-slate-500 dark:bg-slate-900 md:p-8" aria-live="polite">
            <CalendarClock strokeWidth={ICON_STROKE} className="h-7 w-7 animate-pulse text-[#1f6090] dark:text-sky-400" />
            <h2 className="market-morning-serif mt-5 text-2xl font-semibold tracking-[-0.035em] text-[#18263a] dark:text-slate-100">
              本日の朝刊を確認しています
            </h2>
            <p className="mt-3 text-sm leading-7 text-[#5e6f80] dark:text-slate-300">
              公開状態と出典情報を読み込んでいます。
            </p>
          </section>
        ) : null}
        {activeCount >= 3 && editionFailed ? <EditionStatusPanel state="no_data" /> : null}
        {activeCount >= 3 && !editionLoading && editionResponse?.edition ? (
          <MorningEditionView
            edition={editionResponse.edition}
            editionId={editionResponse.edition_id}
            dataMode={editionResponse.data_mode}
          />
        ) : null}
        {activeCount >= 3 && !editionLoading && editionResponse && !editionResponse.edition ? (
          <EditionStatusPanel
            state={editionResponse.status === "market_holiday" ? "market_holiday" : editionResponse.status === "no_data" ? "no_data" : "not_published"}
          />
        ) : null}
      </div>

      {activeCount >= 3 ? (
        <div className="mt-6 flex flex-wrap items-center justify-between gap-4 border border-[#d9d4c8] bg-[#eee8dc]/70 px-5 py-4 dark:border-slate-800 dark:bg-slate-900/70">
          <p className="text-sm text-[#526578] dark:text-slate-300">
            注目銘柄 <strong className="text-[#18263a] dark:text-slate-100">{activeCount}</strong> 件を朝刊の対象として確認します。
          </p>
          <Link to="/market-morning/app/watchlist" className="inline-flex items-center gap-2 text-sm font-semibold text-[#1f6090] hover:underline dark:text-sky-400">
            注目銘柄を確認
            <ArrowRight strokeWidth={ICON_STROKE} className="h-4 w-4" />
          </Link>
        </div>
      ) : null}
    </div>
  );
}
