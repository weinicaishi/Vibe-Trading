import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, type MarketIndicesResponse, type MarketRange } from "@/lib/api";

export function useMarketIndices(range: MarketRange) {
  const [data, setData] = useState<MarketIndicesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [revision, setRevision] = useState(0);
  const requestId = useRef(0);

  const retry = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    const currentRequest = ++requestId.current;
    setLoading(true);
    setError(null);
    setDisabled(false);

    api.getMarketIndices(range, controller.signal)
      .then((response) => {
        if (requestId.current !== currentRequest) return;
        setData(response);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted || requestId.current !== currentRequest) return;
        if (
          reason instanceof ApiError
          && reason.status === 503
          && reason.message === "Market indices feature is disabled"
        ) {
          setDisabled(true);
          return;
        }
        setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted && requestId.current === currentRequest) {
          setLoading(false);
        }
      });

    return () => controller.abort();
  }, [range, revision]);

  return { data, loading, error, disabled, retry };
}
