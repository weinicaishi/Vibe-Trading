import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { IndexCard } from "../IndexCard";
import type { MarketIndexItem } from "@/lib/api";

const item: MarketIndexItem = {
  symbol: "SP500.INDEX",
  name: "S&P 500",
  name_ja: "S&P 500種株価指数",
  market: "global_index",
  region: "US",
  currency: "USD",
  timezone: "America/New_York",
  source: "yahoo",
  delay_status: "unknown",
  tradable: false,
  window_start: "2026-06-17",
  window_end: "2026-07-17",
  latest: { session_date: "2026-07-16", open: 6200, high: 6210, low: 6190, close: 6205 },
  previous_close: 6180,
  change: 25,
  change_percent: 25 / 6180,
  series: [],
};

it("shows point values without currency and exposes selection semantics", async () => {
  const onSelect = vi.fn();
  render(<IndexCard item={item} selected locale="en-US" onSelect={onSelect} />);
  const button = screen.getByRole("button", { name: /S&P 500/ });
  expect(button).toHaveAttribute("aria-selected", "true");
  expect(button).toHaveTextContent("6,205");
  expect(button).not.toHaveTextContent("$");
  await userEvent.click(button);
  expect(onSelect).toHaveBeenCalledWith("SP500.INDEX");
});
