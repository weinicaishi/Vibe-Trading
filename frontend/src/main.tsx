import './i18n';
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";
import { Toaster } from "sonner";
import { ErrorBoundary } from "./components/common/ErrorBoundary";
import { configureMarketMorningAuth0 } from "./lib/marketMorningAuth0";
import { loadMarketMorningRuntimeConfig } from "./lib/marketMorningRuntimeConfig";
import { router } from "./router";
// Self-hosted fonts (VT-006): vendor the woff2 files locally instead of the
// Google Fonts CDN. Weights match tailwind.config.ts (Inter 400/500/600/700,
// JetBrains Mono 400/500/700).
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "@fontsource/jetbrains-mono/700.css";
import "highlight.js/styles/github-dark-dimmed.min.css";
import "./index.css";

async function bootstrap(): Promise<void> {
  // Production reads public Auth0 SPA identifiers from the backend at runtime,
  // keeping one immutable image deployable across environments. Development
  // may fall back to VITE_* values so the standalone Vite workflow remains
  // usable when the API is intentionally absent.
  await loadMarketMorningRuntimeConfig({
    allowBuildTimeFallback: import.meta.env.DEV,
  });

  // Registration still happens before React mounts. The Auth0 SDK initializes
  // lazily behind each private product/operator boundary, so a provider outage
  // cannot take down unrelated Vibe-Trading routes.
  configureMarketMorningAuth0();

  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <ErrorBoundary>
        <RouterProvider router={router} />
        <Toaster position="bottom-right" richColors closeButton duration={3500} />
      </ErrorBoundary>
    </StrictMode>
  );
}

void bootstrap();
