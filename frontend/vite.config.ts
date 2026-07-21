import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import type { IncomingMessage } from "node:http";
import type { Plugin } from "vite";
import {
  createMarketMorningDemoState,
  handleMarketMorningDemoRequest,
  isMarketMorningDemoEnabled,
  MARKET_MORNING_DEV_API_PROXY_PATTERN,
} from "./src/dev/marketMorningDemo";

const PROXY_PATHS = [
  "/sessions",
  "/swarm/presets",
  "/swarm/runs",
  "/qveris",
  "/settings/llm",
  "/settings/data-sources",
  "/channels",
  "/mandate",
  "/live",
  "/upload",
  "/shadow-reports",
];

async function readJsonBody(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += value.length;
    if (size > 64 * 1024) throw new Error("demo_request_too_large");
    chunks.push(value);
  }
  if (chunks.length === 0) return undefined;
  return JSON.parse(Buffer.concat(chunks).toString("utf-8"));
}

function marketMorningDemoPlugin(): Plugin {
  const state = createMarketMorningDemoState();
  return {
    name: "market-morning-local-browser-demo",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        if (!request.url?.startsWith("/market-morning/")) return next();
        try {
          const method = request.method ?? "GET";
          const body = ["POST", "PUT", "PATCH"].includes(method)
            ? await readJsonBody(request)
            : undefined;
          const result = handleMarketMorningDemoRequest(state, {
            method,
            url: request.url,
            body,
          });
          if (result === null) return next();
          response.statusCode = result.status;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");
          response.end(JSON.stringify(result.body));
        } catch {
          response.statusCode = 400;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");
          response.end(JSON.stringify({ detail: "demo_request_invalid" }));
        }
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const demoEnabled = isMarketMorningDemoEnabled(
    mode,
    env.VITE_MARKET_MORNING_DEMO,
  );
  const apiTarget = env.VITE_API_URL || "http://127.0.0.1:8899";
  const apiProxy = { target: apiTarget, changeOrigin: true };
  const apiProxyWithHtmlFallback = {
    ...apiProxy,
    bypass(req: { headers: { accept?: string } }) {
      if (req.headers.accept?.includes("text/html")) {
        return "/index.html";
      }
    },
  };

  return {
    plugins: [react(), ...(demoEnabled ? [marketMorningDemoPlugin()] : [])],
    resolve: {
      alias: { "@": path.resolve(__dirname, "./src") },
    },
    server: {
      port: 5899,
      proxy: {
        ...Object.fromEntries(PROXY_PATHS.map((p) => [p, apiProxy])),
        "^/market(?:/|$)": apiProxy,
        [MARKET_MORNING_DEV_API_PROXY_PATTERN]: apiProxy,
        // SPA RunDetail page — only the two-segment ``/runs/{id}``
        // form should fall back to ``index.html`` on browser navigation.
        // ``/runs/{id}/code`` and ``/runs/{id}/pine`` are API-only and
        // must keep proxying to the backend even when Accept is text/html.
        "^/runs/[^/]+/?$": apiProxyWithHtmlFallback,
        "/runs": apiProxy,
        "/correlation": apiProxyWithHtmlFallback,
        "^/alpha(?:/|$)": apiProxy,
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            "vendor-react": ["react", "react-dom", "react-router-dom"],
            "vendor-charts": ["echarts"],
          },
        },
      },
    },
  };
});
