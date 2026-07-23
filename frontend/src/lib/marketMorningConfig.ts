import { getMarketMorningFrontendEnvironment } from "@/lib/marketMorningRuntimeConfig";

export function isMarketMorningUiEnabled(): boolean {
  return (
    getMarketMorningFrontendEnvironment().VITE_MARKET_MORNING_ENABLED === "true"
  );
}
