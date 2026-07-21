export function isMarketMorningUiEnabled(): boolean {
  return import.meta.env.VITE_MARKET_MORNING_ENABLED === "true";
}
