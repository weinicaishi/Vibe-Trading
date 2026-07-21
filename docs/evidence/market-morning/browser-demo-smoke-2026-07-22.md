# Market Morning local browser Demo smoke

- Date: 2026-07-22 (Asia/Shanghai)
- Scope: `local_browser_synthetic`
- Command: `cd frontend && npm run dev:market-morning-demo`
- URL: `http://127.0.0.1:5901/market-morning`
- Fixture: `local-browser-demo-v1`
- Counts as staging day: `false`
- Counts as T1 evidence: `false`

## Environment boundaries

- The middleware is installed only when Vite mode is exactly `development` and
  `VITE_MARKET_MORNING_DEMO=true`.
- All state is process memory and resets when the Vite server stops.
- The edition is labeled `data_mode=synthetic_fixture`; the UI shows
  `デモデータ` and uses `example.invalid` source links.
- Production build completed and contained none of
  `local-browser-demo-v1`, `market-morning-local-browser-demo`,
  `synthetic_source_unavailable`, or `TD-7203-001`.

## Browser checks

Playwright CLI named session `mm-demo` verified:

1. Today edition showed a corrected Toyota event, a Sony source-unavailable
   state, and `不足以判断` without a buy/sell instruction.
2. Watchlist started at 3/10, searched exact code `9432`, returned NTT, and
   added it to reach 4/10.
3. Toyota research showed event version 2, corrected lifecycle and a fixture
   source; a private note was changed and the UI displayed `保存しました`.
4. Email notification opt-in was enabled and the settings UI displayed
   `保存しました`.

Observed API requests all returned HTTP 200:

- `GET /market-morning/settings`
- `GET /market-morning/watchlist`
- `GET /market-morning/edition/today`
- `GET /market-morning/issuers/search?q=9432&limit=10`
- `POST /market-morning/watchlist`
- `GET /market-morning/issuers/{issuer_id}/research?limit=50`
- `PUT /market-morning/issuers/{issuer_id}/note`
- `PATCH /market-morning/settings`

Browser console result: 0 errors, 0 warnings.

Both 1280x720 screenshots were visually inspected: the fixed navigation,
synthetic warning, edition cards, settings form and saved-state feedback were
legible with no overlap, clipping or broken layout in the captured viewport.

## Artifacts

- Today screenshot:
  `output/playwright/market-morning-demo/.playwright-cli/page-2026-07-21T17-44-13-321Z.png`
- Settings-success screenshot:
  `output/playwright/market-morning-demo/.playwright-cli/page-2026-07-21T17-51-01-553Z.png`
- Per-step accessibility snapshots are stored in the same `.playwright-cli/`
  directory.

## Regression baseline

- Frontend: 341 passed.
- TypeScript + Vite production build: passed.
- Market Morning direct backend scope: 809 passed, 15 skipped.
- Repository backend excluding `agent/tests/e2e_backtest`: 6241 passed,
  24 skipped.
