import type { MarketIndexItem } from "@/lib/api";
import { formatMarketNumber } from "@/lib/formatters";

interface Props {
  item: MarketIndexItem;
  selected: boolean;
  locale: string;
  onSelect: (symbol: string) => void;
}

export function IndexCard({ item, selected, locale, onSelect }: Props) {
  const change = item.change;
  const percent = item.change_percent;
  const direction = change == null ? "neutral" : change > 0 ? "positive" : change < 0 ? "negative" : "neutral";
  const sign = change != null && change > 0 ? "+" : "";
  const changeLabel = change == null || percent == null
    ? "—"
    : `${sign}${formatMarketNumber(change, locale)} (${sign}${(percent * 100).toFixed(2)}%)`;

  return (
    <button
      type="button"
      aria-selected={selected}
      aria-label={`${item.name}: ${changeLabel}, ${direction}`}
      onClick={() => onSelect(item.symbol)}
      className={`rounded-xl border p-4 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary ${
        selected ? "border-primary bg-primary/5 shadow-sm" : "bg-card hover:border-primary/50"
      }`}
    >
      <span className="block text-xs font-medium text-muted-foreground">
        {locale.startsWith("ja") ? item.name_ja : item.name}
      </span>
      <span className="mt-2 block text-xl font-semibold tabular-nums">
        {formatMarketNumber(item.latest.close, locale)}
      </span>
      <span
        className={`mt-1 block text-sm tabular-nums ${
          direction === "positive" ? "text-emerald-600 dark:text-emerald-400" :
          direction === "negative" ? "text-red-600 dark:text-red-400" : "text-muted-foreground"
        }`}
      >
        {changeLabel}
      </span>
    </button>
  );
}
