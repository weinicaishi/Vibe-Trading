import { afterEach, describe, expect, it } from "vitest";
import { consumeMarketMorningDeliveryToken } from "@/lib/marketMorningDeliveryLink";

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("Market Morning delivery link", () => {
  it("returns a valid fragment token and immediately removes it from the address bar", () => {
    const token = "A".repeat(43);
    window.history.replaceState(null, "", `/market-morning?edition=today#token=${token}`);

    expect(consumeMarketMorningDeliveryToken()).toBe(token);
    expect(window.location.pathname).toBe("/market-morning");
    expect(window.location.search).toBe("?edition=today");
    expect(window.location.hash).toBe("");
  });

  it("removes an invalid delivery token without returning or persisting it", () => {
    window.history.replaceState(null, "", "/market-morning#token=invalid-token");

    expect(consumeMarketMorningDeliveryToken()).toBeNull();
    expect(window.location.hash).toBe("");
    expect(JSON.stringify(window.localStorage)).not.toContain("invalid-token");
    expect(JSON.stringify(window.sessionStorage)).not.toContain("invalid-token");
  });

  it("does not clear unrelated fragments", () => {
    window.history.replaceState(null, "", "/market-morning#research");

    expect(consumeMarketMorningDeliveryToken()).toBeNull();
    expect(window.location.hash).toBe("#research");
  });
});
