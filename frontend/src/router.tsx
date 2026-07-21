import { Suspense, lazy, type ComponentType } from "react";
import { createBrowserRouter } from "react-router-dom";
import { Layout } from "@/components/layout/Layout";

const Home = lazy(() => import("@/pages/Home").then((m) => ({ default: m.Home })));
const Agent = lazy(() => import("@/pages/Agent").then((m) => ({ default: m.Agent })));
const RunDetail = lazy(() =>
  import("@/pages/RunDetail").then((m) => ({ default: m.RunDetail })),
);
const Compare = lazy(() =>
  import("@/pages/Compare").then((m) => ({ default: m.Compare })),
);
const Settings = lazy(() =>
  import("@/pages/Settings").then((m) => ({ default: m.Settings })),
);
const Runtime = lazy(() =>
  import("@/pages/Runtime").then((m) => ({ default: m.Runtime })),
);
const Reports = lazy(() =>
  import("@/pages/Reports").then((m) => ({ default: m.Reports })),
);
const Correlation = lazy(() =>
  import("@/pages/Correlation").then((m) => ({ default: m.Correlation })),
);
const AlphaZoo = lazy(() =>
  import("@/pages/AlphaZoo").then((m) => ({ default: m.AlphaZoo })),
);
const MarketMorningShell = lazy(() =>
  import("@/pages/market-morning/MarketMorningShell").then((m) => ({ default: m.MarketMorningShell })),
);
const MarketMorningToday = lazy(() =>
  import("@/pages/market-morning/MarketMorningToday").then((m) => ({ default: m.MarketMorningToday })),
);
const MarketMorningWatchlist = lazy(() =>
  import("@/pages/market-morning/MarketMorningWatchlist").then((m) => ({ default: m.MarketMorningWatchlist })),
);
const MarketMorningSettings = lazy(() =>
  import("@/pages/market-morning/MarketMorningSettings").then((m) => ({ default: m.MarketMorningSettings })),
);
const MarketMorningIssuerResearch = lazy(() =>
  import("@/pages/market-morning/MarketMorningIssuerResearch").then((m) => ({ default: m.MarketMorningIssuerResearch })),
);
const MarketMorningOperations = lazy(() =>
  import("@/pages/MarketMorningOperations").then((m) => ({ default: m.MarketMorningOperations })),
);

function PageLoader() {
  return (
    <div className="flex h-[60vh] items-center justify-center text-muted-foreground">
      Loading…
    </div>
  );
}

function wrap(Component: ComponentType) {
  return (
    <Suspense fallback={<PageLoader />}>
      <Component />
    </Suspense>
  );
}

export const router = createBrowserRouter([
  {
    path: "/market-morning",
    element: wrap(MarketMorningShell),
    children: [
      { index: true, element: wrap(MarketMorningToday) },
      { path: "app/watchlist", element: wrap(MarketMorningWatchlist) },
      { path: "app/issuers/:issuerId", element: wrap(MarketMorningIssuerResearch) },
      { path: "app/settings", element: wrap(MarketMorningSettings) },
    ],
  },
  {
    element: <Layout />,
    children: [
      { path: "/", element: wrap(Home) },
      { path: "/agent", element: wrap(Agent) },
      { path: "/runtime", element: wrap(Runtime) },
      { path: "/market-morning-ops", element: wrap(MarketMorningOperations) },
      { path: "/reports", element: wrap(Reports) },
      { path: "/settings", element: wrap(Settings) },
      { path: "/runs/:runId", element: wrap(RunDetail) },
      { path: "/compare", element: wrap(Compare) },
      { path: "/correlation", element: wrap(Correlation) },
      { path: "/alpha-zoo", element: wrap(AlphaZoo) },
      { path: "/alpha-zoo/bench", element: wrap(AlphaZoo) },
      { path: "/alpha-zoo/compare", element: wrap(AlphaZoo) },
      { path: "/alpha-zoo/:alphaId", element: wrap(AlphaZoo) },
    ],
  },
]);
