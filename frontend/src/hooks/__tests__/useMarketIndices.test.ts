import { act, renderHook, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { ApiError, api, type MarketIndicesResponse } from "@/lib/api";
import { useMarketIndices } from "../useMarketIndices";

const response: MarketIndicesResponse = {
  as_of: "2026-07-17T08:00:00Z",
  range: "1m",
  interval: "1D",
  items: [],
  errors: [],
};

it("fetches by range and retries without changing range", async () => {
  const spy = vi.spyOn(api, "getMarketIndices").mockResolvedValue(response);
  const { result } = renderHook(() => useMarketIndices("1m"));
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(spy).toHaveBeenCalledWith("1m", expect.any(AbortSignal));
  act(() => result.current.retry());
  await waitFor(() => expect(spy).toHaveBeenCalledTimes(2));
  spy.mockRestore();
});

it("aborts the stale request when range changes", async () => {
  const signals: AbortSignal[] = [];
  const spy = vi.spyOn(api, "getMarketIndices").mockImplementation((_range, signal) => {
    if (signal) signals.push(signal);
    return new Promise(() => undefined);
  });
  const { rerender, unmount } = renderHook(({ range }) => useMarketIndices(range), {
    initialProps: { range: "1m" as const },
  });
  rerender({ range: "3m" as const });
  expect(signals[0].aborted).toBe(true);
  unmount();
  expect(signals[1].aborted).toBe(true);
  spy.mockRestore();
});

it("reports the rollout flag as disabled instead of a user-facing outage", async () => {
  const spy = vi.spyOn(api, "getMarketIndices").mockRejectedValue(
    new ApiError("Market indices feature is disabled", 503),
  );
  const { result } = renderHook(() => useMarketIndices("1m"));
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.disabled).toBe(true);
  expect(result.current.error).toBeNull();
  spy.mockRestore();
});
