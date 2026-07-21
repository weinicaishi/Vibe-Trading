import { useEffect, useRef } from "react";
import type { MarketBar } from "@/lib/api";
import { useDarkMode } from "@/hooks/useDarkMode";
import { getChartTheme } from "@/lib/chart-theme";
import { echarts } from "@/lib/echarts";
import { formatMarketNumber } from "@/lib/formatters";

interface Props {
  series: MarketBar[];
  name: string;
  locale: string;
}

export function IndexChart({ series, name, locale }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const { dark } = useDarkMode();

  useEffect(() => {
    if (!ref.current || series.length === 0) return;
    const theme = getChartTheme();
    const chart = echarts.init(ref.current);
    chart.setOption({
      backgroundColor: "transparent",
      grid: { left: 12, right: 16, top: 20, bottom: 24, containLabel: true },
      tooltip: {
        trigger: "axis",
        backgroundColor: theme.tooltipBg,
        borderColor: theme.tooltipBorder,
        textStyle: { color: theme.tooltipText },
        formatter: (params: unknown) => {
          const first = Array.isArray(params) ? params[0] : null;
          if (!first || typeof first !== "object") return "";
          const point = first as { axisValue?: string; value?: number };
          return `${point.axisValue ?? ""}<br/>${name}: <b>${formatMarketNumber(Number(point.value), locale)}</b>`;
        },
      },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: series.map((bar) => bar.session_date),
        axisLine: { lineStyle: { color: theme.axisColor } },
        axisLabel: { color: theme.textColor, hideOverlap: true },
      },
      yAxis: {
        type: "value",
        scale: true,
        splitLine: { lineStyle: { color: theme.gridColor } },
        axisLabel: { color: theme.textColor, formatter: (value: number) => formatMarketNumber(value, locale) },
      },
      series: [{
        name,
        type: "line",
        data: series.map((bar) => bar.close),
        symbol: "none",
        lineStyle: { width: 2, color: theme.infoColor },
        areaStyle: { color: theme.infoColor + "18" },
      }],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [dark, locale, name, series]);

  return <div ref={ref} className="h-72 w-full" role="img" aria-label={`${name} close chart`} />;
}
