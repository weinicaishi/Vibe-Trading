import { useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { MarketRange } from "@/lib/api";
import { useMarketIndices } from "@/hooks/useMarketIndices";
import { IndexCard } from "./IndexCard";
import { IndexChart } from "./IndexChart";
import { MarketDataNotice } from "./MarketDataNotice";

const RANGES: MarketRange[] = ["1m", "3m", "6m", "1y"];

export function IndexOverview() {
  const { t, i18n } = useTranslation();
  const locale = i18n.resolvedLanguage || i18n.language || "ja";
  const [range, setRange] = useState<MarketRange>("1m");
  const [selectedSymbol, setSelectedSymbol] = useState("NIKKEI225.INDEX");
  const { data, loading, error, disabled, retry } = useMarketIndices(range);

  useEffect(() => {
    if (!data?.items.length) return;
    if (!data.items.some((item) => item.symbol === selectedSymbol)) {
      setSelectedSymbol(data.items[0].symbol);
    }
  }, [data, selectedSymbol]);

  const selected = useMemo(
    () => data?.items.find((item) => item.symbol === selectedSymbol) ?? data?.items[0],
    [data, selectedSymbol],
  );

  if (disabled) return null;

  return (
    <section className="mt-16 w-full max-w-5xl" aria-labelledby="market-overview-title">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-primary">{t("market.eyebrow")}</p>
          <h2 id="market-overview-title" className="mt-1 text-2xl font-semibold">{t("market.title")}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{t("market.subtitle")}</p>
        </div>
        <div className="flex rounded-lg border bg-muted/30 p-1" aria-label={t("market.rangeLabel")}>
          {RANGES.map((value) => (
            <button
              type="button"
              key={value}
              onClick={() => setRange(value)}
              aria-pressed={range === value}
              className={`rounded-md px-3 py-1.5 text-xs font-medium uppercase ${range === value ? "bg-background shadow-sm" : "text-muted-foreground"}`}
            >
              {value}
            </button>
          ))}
        </div>
      </div>

      {loading && !data && (
        <div className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4" aria-label={t("market.loading")}>
          {RANGES.map((value) => <div key={value} className="h-28 animate-pulse rounded-xl border bg-muted/40" />)}
        </div>
      )}

      {error && !data && (
        <div className="mt-6 rounded-xl border border-destructive/30 bg-destructive/5 p-5">
          <p className="font-medium">{t("market.unavailable")}</p>
          <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          <button type="button" onClick={retry} className="mt-3 inline-flex items-center gap-2 text-sm font-medium text-primary">
            <RefreshCw className="h-4 w-4" /> {t("market.retry")}
          </button>
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <div className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {data.items.map((item) => (
              <IndexCard key={item.symbol} item={item} selected={item.symbol === selected?.symbol} locale={locale} onSelect={setSelectedSymbol} />
            ))}
          </div>
          {data.errors.length > 0 && <p className="mt-3 text-sm text-amber-700 dark:text-amber-300">{t("market.partial")}</p>}
          {selected && (
            <div className="mt-4 rounded-xl border bg-card p-4 sm:p-6">
              <IndexChart series={selected.series} name={locale.startsWith("ja") ? selected.name_ja : selected.name} locale={locale} />
              <MarketDataNotice item={selected} asOf={data.as_of} locale={locale} />
            </div>
          )}
        </>
      )}
    </section>
  );
}
