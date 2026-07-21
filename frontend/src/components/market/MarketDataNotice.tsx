import { useTranslation } from "react-i18next";
import type { MarketIndexItem } from "@/lib/api";
import { formatMarketDateTime } from "@/lib/formatters";

interface Props {
  item: MarketIndexItem;
  asOf: string;
  locale: string;
}

export function MarketDataNotice({ item, asOf, locale }: Props) {
  const { t } = useTranslation();
  return (
    <p className="text-xs leading-relaxed text-muted-foreground">
      {t("market.source")}: {item.source} · {t("market.session")}: {item.latest.session_date} · {item.timezone} · {t("market.updated")}: {formatMarketDateTime(asOf, locale, item.timezone)} · {t("market.delayUnknown")}. {t("market.disclaimer")}
    </p>
  );
}
