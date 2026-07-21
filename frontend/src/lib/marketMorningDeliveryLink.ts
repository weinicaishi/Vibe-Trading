const DELIVERY_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;

export function consumeMarketMorningDeliveryToken(
  location: Pick<Location, "hash" | "pathname" | "search"> = window.location,
  history: Pick<History, "replaceState" | "state"> = window.history,
): string | null {
  if (!location.hash) return null;

  const parameters = new URLSearchParams(location.hash.slice(1));
  if (!parameters.has("token")) return null;

  const token = parameters.get("token");
  history.replaceState(history.state, "", `${location.pathname}${location.search}`);
  return token !== null && DELIVERY_TOKEN_PATTERN.test(token) ? token : null;
}
