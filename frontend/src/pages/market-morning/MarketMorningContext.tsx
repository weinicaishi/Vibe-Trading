import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  classifyMarketMorningAccessError,
  marketMorningApi,
  type MarketMorningAccessState,
  type MarketMorningSettings,
  type WatchlistResponse,
} from "@/lib/marketMorningApi";

type BootStatus = "loading" | "ready" | "access_error" | "error";

interface MarketMorningContextValue {
  status: BootStatus;
  accessState: MarketMorningAccessState | null;
  errorMessage: string | null;
  settings: MarketMorningSettings | null;
  watchlist: WatchlistResponse | null;
  reload: () => Promise<void>;
  setSettings: (settings: MarketMorningSettings) => void;
  setWatchlist: (watchlist: WatchlistResponse) => void;
}

const MarketMorningContext = createContext<MarketMorningContextValue | null>(null);

export function MarketMorningProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<BootStatus>("loading");
  const [accessState, setAccessState] = useState<MarketMorningAccessState | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [settings, setSettings] = useState<MarketMorningSettings | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistResponse | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setStatus("loading");
    setAccessState(null);
    setErrorMessage(null);
    try {
      const [nextSettings, nextWatchlist] = await Promise.all([
        marketMorningApi.getSettings(signal),
        marketMorningApi.getWatchlist(signal),
      ]);
      setSettings(nextSettings);
      setWatchlist(nextWatchlist);
      setStatus("ready");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      const classified = classifyMarketMorningAccessError(error);
      setSettings(null);
      setWatchlist(null);
      if (classified !== "unknown") {
        setAccessState(classified);
        setStatus("access_error");
      } else {
        setErrorMessage(error instanceof Error ? error.message : "Unknown error");
        setStatus("error");
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const reload = useCallback(async () => {
    await load();
  }, [load]);

  const value = useMemo<MarketMorningContextValue>(
    () => ({
      status,
      accessState,
      errorMessage,
      settings,
      watchlist,
      reload,
      setSettings,
      setWatchlist,
    }),
    [accessState, errorMessage, reload, settings, status, watchlist],
  );

  return <MarketMorningContext.Provider value={value}>{children}</MarketMorningContext.Provider>;
}

export function useMarketMorning() {
  const value = useContext(MarketMorningContext);
  if (!value) throw new Error("useMarketMorning must be used inside MarketMorningProvider");
  return value;
}
