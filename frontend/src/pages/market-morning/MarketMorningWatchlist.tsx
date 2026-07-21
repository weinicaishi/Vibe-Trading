import { useState, type FormEvent } from "react";
import { ArrowRight, Check, Plus, Search, Trash2, X } from "lucide-react";
import { Link } from "react-router-dom";
import {
  MarketMorningApiError,
  marketMorningApi,
  type IssuerSearchItem,
} from "@/lib/marketMorningApi";
import { useMarketMorning } from "@/pages/market-morning/MarketMorningContext";

const ICON_STROKE = 1.7;

function messageFor(error: unknown): string {
  if (error instanceof MarketMorningApiError) return error.message;
  return "処理に失敗しました。もう一度お試しください。";
}

export function MarketMorningWatchlist() {
  const { watchlist, setWatchlist } = useMarketMorning();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<IssuerSearchItem[]>([]);
  const [guidance, setGuidance] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [mutatingId, setMutatingId] = useState<string | null>(null);
  const [removeTarget, setRemoveTarget] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const activeItems = watchlist?.items ?? [];
  const activeCount = watchlist?.active_count ?? activeItems.length;
  const limit = watchlist?.limit ?? 10;
  const activeIssuerIds = new Set(activeItems.map((item) => item.issuer_id));

  const search = async (event: FormEvent) => {
    event.preventDefault();
    const value = query.trim();
    if (!value) return;
    setSearching(true);
    setError(null);
    try {
      const response = await marketMorningApi.searchIssuers(value);
      setResults(response.items);
      setGuidance(response.no_match_guidance);
    } catch (nextError) {
      setResults([]);
      setGuidance(null);
      setError(messageFor(nextError));
    } finally {
      setSearching(false);
    }
  };

  const add = async (issuerId: string) => {
    if (!watchlist) return;
    setMutatingId(issuerId);
    setError(null);
    try {
      const response = await marketMorningApi.addWatchlistItem(issuerId);
      if (response.item && !activeIssuerIds.has(response.item.issuer_id)) {
        setWatchlist({
          ...watchlist,
          items: [...watchlist.items, response.item].sort((a, b) => a.sort_order - b.sort_order),
          active_count: response.active_count,
          limit: response.limit,
        });
      }
    } catch (nextError) {
      setError(messageFor(nextError));
    } finally {
      setMutatingId(null);
    }
  };

  const remove = async (issuerId: string) => {
    if (!watchlist) return;
    setMutatingId(issuerId);
    setError(null);
    try {
      const response = await marketMorningApi.removeWatchlistItem(issuerId);
      setWatchlist({
        ...watchlist,
        items: watchlist.items.filter((item) => item.issuer_id !== issuerId),
        active_count: response.active_count,
        limit: response.limit,
      });
      setRemoveTarget(null);
    } catch (nextError) {
      setError(messageFor(nextError));
    } finally {
      setMutatingId(null);
    }
  };

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-8 md:px-8 md:py-10">
      <header className="border-b border-[#d9d4c8] pb-7 dark:border-slate-800">
        <p className="font-mono text-[10px] tracking-[0.08em] text-[#1f6090] dark:text-sky-400">WATCHLIST</p>
        <h1 className="market-morning-serif mt-3 text-3xl font-semibold tracking-[-0.045em] text-[#18263a] dark:text-slate-100 md:text-4xl">
          毎朝読む会社を選ぶ。
        </h1>
        <p className="mt-3 max-w-[62ch] text-sm leading-7 text-[#5e6f80] dark:text-slate-300">
          証券コードまたは正式な会社名で検索します。意味を推測した自動候補は表示しません。
        </p>
      </header>

      <div className="mt-8 grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.72fr)]">
        <section>
          <form onSubmit={search} className="flex gap-2" role="search">
            <label className="sr-only" htmlFor="issuer-search">会社名または証券コード</label>
            <div className="relative min-w-0 flex-1">
              <Search strokeWidth={ICON_STROKE} className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[#6f7d89]" />
              <input
                id="issuer-search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="例: 7203 または トヨタ自動車"
                maxLength={100}
                className="h-11 w-full border border-[#bfb9ad] bg-[#fffdf8] pl-10 pr-3 text-sm text-[#18263a] outline-none transition placeholder:text-[#8a918f] focus:border-[#1f6090] focus:ring-2 focus:ring-[#1f6090]/20 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:border-sky-500"
              />
            </div>
            <button
              type="submit"
              disabled={searching || !query.trim()}
              className="h-11 whitespace-nowrap bg-[#1f6090] px-4 text-sm font-semibold text-white transition hover:bg-[#184f78] active:translate-y-px disabled:cursor-not-allowed disabled:opacity-50 dark:bg-sky-600 dark:hover:bg-sky-500"
            >
              {searching ? "検索中" : "検索"}
            </button>
          </form>

          {error ? (
            <div role="alert" className="mt-4 border-l-2 border-[#ad4d47] bg-[#f8e8e4] px-4 py-3 text-sm text-[#884540] dark:bg-rose-950/40 dark:text-rose-200">
              {error}
            </div>
          ) : null}

          <div className="mt-5 border-t border-[#18263a] dark:border-slate-600">
            {results.length === 0 && !searching ? (
              <div className="bg-[#fffdf8] px-5 py-10 text-center dark:bg-slate-900">
                <p className="text-sm font-medium text-[#33485b] dark:text-slate-200">
                  {guidance ?? "検索結果はここに表示されます。"}
                </p>
                <p className="mt-2 text-xs leading-6 text-[#74818c] dark:text-slate-500">
                  正式名称と承認済みの別名だけを検索します。
                </p>
              </div>
            ) : null}
            {results.map((issuer) => {
              const active = activeIssuerIds.has(issuer.issuer_id);
              const disabled = active || activeCount >= limit || mutatingId === issuer.issuer_id;
              return (
                <article key={issuer.issuer_id} className="grid grid-cols-[1fr_auto] gap-4 border-b border-[#d9d4c8] bg-[#fffdf8] px-4 py-4 dark:border-slate-800 dark:bg-slate-900">
                  <div className="min-w-0">
                    <h2 className="truncate text-sm font-semibold text-[#18263a] dark:text-slate-100">{issuer.legal_name_ja}</h2>
                    <p className="mt-1 font-mono text-[10px] text-[#697886] dark:text-slate-500">
                      {issuer.issuer_code} / {issuer.market_segment}
                    </p>
                    {issuer.matched_alias ? (
                      <p className="mt-2 text-xs text-[#5e6f80] dark:text-slate-400">一致した名称: {issuer.matched_alias}</p>
                    ) : null}
                  </div>
                  <button
                    type="button"
                    onClick={() => void add(issuer.issuer_id)}
                    disabled={disabled}
                    className="inline-flex h-9 items-center gap-1.5 self-center whitespace-nowrap border border-[#1f6090] px-3 text-xs font-semibold text-[#1f6090] transition hover:bg-[#e8f0f2] active:translate-y-px disabled:cursor-not-allowed disabled:border-[#c9c4b8] disabled:text-[#8b918f] disabled:hover:bg-transparent dark:border-sky-500 dark:text-sky-400 dark:hover:bg-sky-950 dark:disabled:border-slate-700 dark:disabled:text-slate-600"
                  >
                    {active ? <Check strokeWidth={ICON_STROKE} className="h-3.5 w-3.5" /> : <Plus strokeWidth={ICON_STROKE} className="h-3.5 w-3.5" />}
                    {active ? "登録済み" : "追加"}
                  </button>
                </article>
              );
            })}
          </div>
        </section>

        <aside>
          <div className="flex items-end justify-between border-b border-[#18263a] pb-3 dark:border-slate-600">
            <div>
              <p className="text-sm font-semibold text-[#18263a] dark:text-slate-100">注目銘柄</p>
              <p className="mt-1 text-xs text-[#697886] dark:text-slate-500">朝刊の確認対象</p>
            </div>
            <p className="font-mono text-sm text-[#1f6090] dark:text-sky-400">{activeCount} / {limit}</p>
          </div>

          {activeItems.length === 0 ? (
            <div className="mt-4 border border-[#d9d4c8] bg-[#eee8dc]/70 p-5 dark:border-slate-800 dark:bg-slate-900">
              <p className="text-sm font-medium text-[#33485b] dark:text-slate-200">まだ銘柄がありません。</p>
              <p className="mt-2 text-xs leading-6 text-[#697886] dark:text-slate-500">左の検索から、毎朝確認したい会社を追加してください。</p>
            </div>
          ) : (
            <div className="mt-2">
              {activeItems.map((item) => (
                <article key={item.issuer_id} className="grid grid-cols-[1fr_auto] items-center gap-3 border-b border-[#d9d4c8] py-4 dark:border-slate-800">
                  <div className="min-w-0">
                    <Link
                      to={`/market-morning/app/issuers/${encodeURIComponent(item.issuer_id)}`}
                      aria-label={`${item.legal_name_ja}の研究を見る`}
                      className="block truncate text-sm font-semibold text-[#18263a] hover:text-[#1f6090] hover:underline dark:text-slate-100 dark:hover:text-sky-400"
                    >
                      {item.legal_name_ja}
                    </Link>
                    <p className="mt-1 font-mono text-[10px] text-[#697886] dark:text-slate-500">{item.issuer_code} / {item.market_segment}</p>
                  </div>
                  {removeTarget === item.issuer_id ? (
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        onClick={() => void remove(item.issuer_id)}
                        disabled={mutatingId === item.issuer_id}
                        className="h-8 whitespace-nowrap bg-[#ad4d47] px-2.5 text-[11px] font-semibold text-white disabled:opacity-50"
                      >
                        削除する
                      </button>
                      <button type="button" onClick={() => setRemoveTarget(null)} className="grid h-8 w-8 place-items-center border border-[#c9c4b8] text-[#667789] dark:border-slate-700 dark:text-slate-400" aria-label="削除をキャンセル">
                        <X strokeWidth={ICON_STROKE} className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ) : (
                    <button type="button" onClick={() => setRemoveTarget(item.issuer_id)} className="grid h-8 w-8 place-items-center text-[#7a8791] transition hover:bg-[#f8e8e4] hover:text-[#ad4d47] dark:hover:bg-rose-950" aria-label={`${item.legal_name_ja}を削除`}>
                      <Trash2 strokeWidth={ICON_STROKE} className="h-4 w-4" />
                    </button>
                  )}
                </article>
              ))}
            </div>
          )}

          {activeCount >= 3 ? (
            <Link to="/market-morning" className="mt-5 inline-flex w-full items-center justify-between bg-[#e2f0ec] px-4 py-3 text-sm font-semibold text-[#15766d] transition hover:bg-[#d5e9e4] active:translate-y-px dark:bg-emerald-950 dark:text-emerald-300">
              朝刊の準備状態を見る
              <ArrowRight strokeWidth={ICON_STROKE} className="h-4 w-4" />
            </Link>
          ) : (
            <p className="mt-5 bg-[#e8f0f2] px-4 py-3 text-xs leading-6 text-[#526b7e] dark:bg-slate-900 dark:text-slate-400">
              あと {Math.max(0, 3 - activeCount)} 銘柄で最初の設定が完了します。
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}
