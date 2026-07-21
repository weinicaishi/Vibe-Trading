import {
  AlertTriangle,
  ArrowLeft,
  ExternalLink,
  FileText,
  NotebookPen,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  MarketMorningApiError,
  marketMorningApi,
  type EditionSourceCitation,
  type IssuerResearchEvent,
  type IssuerResearchResponse,
} from "@/lib/marketMorningApi";

const ICON_STROKE = 1.7;
const NOTE_LIMIT = 1_000;

function formatDate(value: string) {
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo",
    year: "numeric",
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function providerLabel(provider: string) {
  if (provider === "tdnet") return "TDnet";
  if (provider === "edinet") return "EDINET";
  if (provider.startsWith("company_ir")) return "会社IR";
  return provider;
}

function lifecycleLabel(status: IssuerResearchEvent["lifecycle_status"]) {
  if (status === "corrected") return "訂正済み";
  if (status === "withdrawn") return "撤回済み";
  return null;
}

function citationKey(citation: EditionSourceCitation) {
  return `${citation.provider}-${citation.document_id}-${citation.revision_key}`;
}

function EventCard({ event }: { event: IssuerResearchEvent }) {
  const lifecycle = lifecycleLabel(event.lifecycle_status);
  return (
    <article className="border-t-2 border-[#18263a] bg-[#fffdf8] p-5 dark:border-slate-500 dark:bg-slate-900 md:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-mono text-[10px] tracking-[0.08em] text-[#6d7b87] dark:text-slate-500">
            {formatDate(event.occurred_at)} JST / VERSION {event.event_version}
          </p>
          <h2 className="market-morning-serif mt-2 text-xl font-semibold leading-7 tracking-[-0.03em] text-[#18263a] dark:text-slate-100">
            {event.title}
          </h2>
        </div>
        {lifecycle ? (
          <span className="border border-[#b8533c] bg-[#fff0e8] px-2.5 py-1 text-[11px] font-semibold text-[#8f3828] dark:border-orange-600 dark:bg-orange-950 dark:text-orange-300">
            {lifecycle}
          </span>
        ) : null}
      </div>

      {event.supersedes_event_id ? (
        <p className="mt-3 text-xs leading-5 text-[#7a665c] dark:text-orange-300/80">
          以前の開示を更新する版です。最新の内容と原資料を確認してください。
        </p>
      ) : null}

      <section className="mt-5" aria-label="確認できた事実">
        <p className="font-mono text-[10px] tracking-[0.08em] text-[#15766d] dark:text-emerald-400">
          確認できた事実
        </p>
        {event.facts.length ? (
          <ul className="mt-3 list-disc space-y-2 pl-5 text-sm font-semibold leading-6 text-[#293e50] dark:text-slate-200">
            {event.facts.map((fact, index) => (
              <li key={`${event.event_id}-${index}-${fact.text}`}>{fact.text}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-3 text-sm leading-6 text-[#687784] dark:text-slate-400">
            要約を確認できないため、公式タイトルと一次情報だけを表示します。
          </p>
        )}
      </section>

      <div className="mt-5 flex flex-wrap gap-x-5 gap-y-2 border-t border-[#d9d4c8] pt-4 dark:border-slate-700">
        {event.citations.map((citation) => (
          <a
            key={citationKey(citation)}
            href={citation.original_url}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex items-center gap-1.5 text-xs font-semibold text-[#1f6090] hover:underline dark:text-sky-400"
          >
            <FileText strokeWidth={ICON_STROKE} className="h-3.5 w-3.5" />
            {providerLabel(citation.provider)}
            <ExternalLink strokeWidth={ICON_STROKE} className="h-3 w-3" />
          </a>
        ))}
      </div>
    </article>
  );
}

export function MarketMorningIssuerResearch() {
  const { issuerId } = useParams<{ issuerId: string }>();
  const [research, setResearch] = useState<IssuerResearchResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [noteText, setNoteText] = useState("");
  const [noteSaving, setNoteSaving] = useState(false);
  const [noteStatus, setNoteStatus] = useState<string | null>(null);

  const load = () => {
    if (!issuerId) {
      setLoading(false);
      setError("銘柄を確認できませんでした。");
      return () => undefined;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    marketMorningApi.getIssuerResearch(issuerId, controller.signal).then(
      (response) => {
        setResearch(response);
        setNoteText(response.note?.text ?? "");
        setLoading(false);
      },
      (nextError: unknown) => {
        if (nextError instanceof DOMException && nextError.name === "AbortError") return;
        setResearch(null);
        setError(
          nextError instanceof MarketMorningApiError && nextError.status === 404
            ? "この銘柄の研究ページを閲覧できません。注目銘柄を確認してください。"
            : "個股情報を読み込めませんでした。",
        );
        setLoading(false);
      },
    );
    return () => controller.abort();
  };

  useEffect(load, [issuerId]);

  const saveNote = async (event: FormEvent) => {
    event.preventDefault();
    if (!issuerId || !noteText.trim()) return;
    setNoteSaving(true);
    setNoteStatus(null);
    try {
      const response = await marketMorningApi.updateIssuerNote(
        issuerId,
        noteText.trim(),
      );
      setResearch((current) =>
        current ? { ...current, note: response.note } : current,
      );
      setNoteText(response.note.text);
      setNoteStatus("保存しました");
    } catch {
      setNoteStatus("保存できませんでした。もう一度お試しください。");
    } finally {
      setNoteSaving(false);
    }
  };

  const clearNote = async () => {
    if (!issuerId) return;
    setNoteSaving(true);
    setNoteStatus(null);
    try {
      await marketMorningApi.deleteIssuerNote(issuerId);
      setResearch((current) =>
        current ? { ...current, note: null } : current,
      );
      setNoteText("");
      setNoteStatus("メモを削除しました");
    } catch {
      setNoteStatus("削除できませんでした。もう一度お試しください。");
    } finally {
      setNoteSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="mx-auto w-full max-w-6xl px-4 py-10 md:px-8" aria-live="polite">
        <RefreshCw strokeWidth={ICON_STROKE} className="h-6 w-6 animate-spin text-[#1f6090] motion-reduce:animate-none" />
        <p className="mt-4 text-sm text-[#5e6f80] dark:text-slate-300">個股履歴を確認しています。</p>
      </div>
    );
  }

  if (error || !research) {
    return (
      <div className="mx-auto w-full max-w-4xl px-4 py-10 md:px-8">
        <section className="border-t-2 border-[#ad4d47] bg-[#fffdf8] p-6 dark:bg-slate-900">
          <AlertTriangle strokeWidth={ICON_STROKE} className="h-6 w-6 text-[#ad4d47]" />
          <h1 className="market-morning-serif mt-4 text-2xl font-semibold text-[#18263a] dark:text-slate-100">個股研究を表示できません</h1>
          <p className="mt-3 text-sm leading-7 text-[#5e6f80] dark:text-slate-300">{error}</p>
          <Link to="/market-morning/app/watchlist" className="mt-5 inline-flex items-center gap-2 text-sm font-semibold text-[#1f6090] hover:underline dark:text-sky-400">
            <ArrowLeft strokeWidth={ICON_STROKE} className="h-4 w-4" />
            注目銘柄へ戻る
          </Link>
        </section>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-8 md:px-8 md:py-10">
      <Link to="/market-morning/app/watchlist" className="inline-flex items-center gap-2 text-xs font-semibold text-[#1f6090] hover:underline dark:text-sky-400">
        <ArrowLeft strokeWidth={ICON_STROKE} className="h-4 w-4" />
        注目銘柄へ戻る
      </Link>

      <header className="mt-5 border-b border-[#d9d4c8] pb-7 dark:border-slate-800">
        <p className="font-mono text-[10px] tracking-[0.08em] text-[#1f6090] dark:text-sky-400">
          {research.issuer_code} / {research.market_segment}
        </p>
        <h1 className="market-morning-serif mt-3 text-3xl font-semibold tracking-[-0.045em] text-[#18263a] dark:text-slate-100 md:text-4xl">
          {research.legal_name_ja}
        </h1>
        <p className="mt-3 max-w-[64ch] text-sm leading-7 text-[#5e6f80] dark:text-slate-300">
          公式開示の履歴を、訂正・撤回と出典を保ったまま確認します。価格予想や売買判断は表示しません。
        </p>
      </header>

      <div className="mt-8 grid gap-8 lg:grid-cols-[minmax(0,1fr)_320px]">
        <section aria-label="公式イベント履歴">
          <div className="flex items-end justify-between border-b border-[#18263a] pb-3 dark:border-slate-600">
            <div>
              <p className="font-mono text-[10px] tracking-[0.08em] text-[#1f6090] dark:text-sky-400">EVENT HISTORY</p>
              <h2 className="mt-1 text-lg font-semibold text-[#18263a] dark:text-slate-100">公式イベント履歴</h2>
            </div>
            <span className="font-mono text-xs text-[#6d7b87] dark:text-slate-500">{research.events.length} 件</span>
          </div>
          {research.events.length ? (
            <div className="mt-5 grid gap-5">
              {research.events.map((event) => <EventCard key={event.event_id} event={event} />)}
            </div>
          ) : (
            <div className="mt-5 border border-[#d9d4c8] bg-[#fffdf8] p-6 dark:border-slate-700 dark:bg-slate-900">
              <p className="text-sm font-semibold text-[#33485b] dark:text-slate-200">確認できる履歴がまだありません</p>
              <p className="mt-2 text-xs leading-6 text-[#697886] dark:text-slate-500">情報がないことと、重要な変化がないことは区別しています。</p>
            </div>
          )}
        </section>

        <aside className="lg:sticky lg:top-6 lg:self-start">
          <form onSubmit={saveNote} className="border-t-2 border-[#1f6090] bg-[#e8f0f2] p-5 dark:border-sky-500 dark:bg-slate-900">
            <NotebookPen strokeWidth={ICON_STROKE} className="h-5 w-5 text-[#1f6090] dark:text-sky-400" />
            <label htmlFor="issuer-note" className="mt-4 block text-sm font-semibold text-[#24384a] dark:text-slate-100">自分用メモ</label>
            <p className="mt-2 text-xs leading-5 text-[#5e6f80] dark:text-slate-400">確認したい論点だけを記録します。AIへの質問や売買指示には使用されません。</p>
            <textarea
              id="issuer-note"
              value={noteText}
              onChange={(event) => setNoteText(event.target.value)}
              maxLength={NOTE_LIMIT}
              rows={7}
              className="mt-4 w-full resize-y border border-[#b8c2c6] bg-[#fffdf8] p-3 text-sm leading-6 text-[#18263a] outline-none focus:border-[#1f6090] focus:ring-2 focus:ring-[#1f6090]/20 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
            />
            <p className="mt-1 text-right font-mono text-[10px] text-[#74818c] dark:text-slate-500">{noteText.length} / {NOTE_LIMIT}</p>
            <div className="mt-4 flex flex-wrap gap-2">
              <button type="submit" disabled={noteSaving || !noteText.trim()} className="bg-[#1f6090] px-3.5 py-2 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50 dark:bg-sky-600">メモを保存</button>
              {research.note ? (
                <button type="button" onClick={() => void clearNote()} disabled={noteSaving} className="inline-flex items-center gap-1.5 border border-[#c9c4b8] px-3 py-2 text-xs font-semibold text-[#6b7377] disabled:opacity-50 dark:border-slate-700 dark:text-slate-400">
                  <Trash2 strokeWidth={ICON_STROKE} className="h-3.5 w-3.5" />
                  メモを削除
                </button>
              ) : null}
            </div>
            {noteStatus ? <p className="mt-3 text-xs text-[#15766d] dark:text-emerald-300" role="status">{noteStatus}</p> : null}
          </form>

          <div className="mt-5 border-l-2 border-[#15766d] bg-[#e2f0ec] p-4 dark:border-emerald-500 dark:bg-emerald-950/40">
            <ShieldCheck strokeWidth={ICON_STROKE} className="h-5 w-5 text-[#15766d] dark:text-emerald-400" />
            <p className="mt-3 text-xs font-semibold text-[#275b55] dark:text-emerald-200">事実と出典を優先</p>
            <p className="mt-1 text-xs leading-6 text-[#55736f] dark:text-emerald-300/80">要約を検証できない場合は、公式タイトルと一次情報だけを表示します。</p>
          </div>
        </aside>
      </div>
    </div>
  );
}
