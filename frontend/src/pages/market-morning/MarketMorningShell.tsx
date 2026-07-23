import { Link, NavLink, Outlet } from "react-router-dom";
import {
  ArrowLeft,
  BookOpenCheck,
  ListChecks,
  LogIn,
  LogOut,
  Moon,
  RefreshCw,
  Settings,
  Sun,
  Sunrise,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useDarkMode } from "@/hooks/useDarkMode";
import {
  beginMarketMorningLogin,
  endMarketMorningSession,
  hasMarketMorningAuthLifecycleAdapter,
  initializeMarketMorningAuth,
  marketMorningAuthHeaders,
} from "@/lib/marketMorningAuth";
import { marketMorningApi } from "@/lib/marketMorningApi";
import { isMarketMorningUiEnabled } from "@/lib/marketMorningConfig";
import { consumeMarketMorningDeliveryToken } from "@/lib/marketMorningDeliveryLink";
import { cn } from "@/lib/utils";
import {
  MarketMorningProvider,
  useMarketMorning,
} from "@/pages/market-morning/MarketMorningContext";

const ICON_STROKE = 1.7;

const NAV_ITEMS = [
  { to: "/market-morning", end: true, label: "今日の朝刊", icon: Sunrise },
  { to: "/market-morning/app/watchlist", end: false, label: "注目銘柄", icon: ListChecks },
  { to: "/market-morning/app/settings", end: false, label: "設定", icon: Settings },
];

function ProductMark() {
  return (
    <Link to="/market-morning" className="flex items-center gap-3 text-current no-underline">
      <span className="relative grid h-9 w-9 place-items-center border border-[#86b7bd] font-mono text-xs text-[#d5eeee]">
        M
        <span className="absolute -right-1 -top-1 h-2.5 w-2.5 bg-[#7db8bf]" />
      </span>
      <span className="min-w-0">
        <strong className="block truncate text-sm font-semibold tracking-[-0.025em] text-white">
          Market Morning
        </strong>
        <small className="block font-mono text-[9px] tracking-[0.12em] text-[#91aab7]">
          RESEARCH DESK
        </small>
      </span>
    </Link>
  );
}

function LoadingState() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-10 md:px-8" aria-label="読み込み中">
      <div className="h-3 w-28 animate-pulse rounded-sm bg-[#d9d4c8] motion-reduce:animate-none dark:bg-slate-700" />
      <div className="mt-5 h-10 w-2/3 animate-pulse rounded-sm bg-[#d9d4c8] motion-reduce:animate-none dark:bg-slate-700" />
      <div className="mt-10 grid gap-px overflow-hidden border border-[#d9d4c8] bg-[#d9d4c8] dark:border-slate-700 dark:bg-slate-700 md:grid-cols-3">
        {[0, 1, 2].map((index) => (
          <div key={index} className="h-32 animate-pulse bg-[#fffdf8] motion-reduce:animate-none dark:bg-slate-900" />
        ))}
      </div>
    </div>
  );
}

const ACCESS_COPY = {
  disabled: {
    title: "Market Morning は現在オフです",
    body: "管理者が機能を有効にするまで、この研究デスクは表示されません。",
  },
  authentication_unconfigured: {
    title: "招待制ログインを準備中です",
    body: "認証サービスの接続後に、招待されたユーザーだけが注目銘柄と設定へアクセスできます。",
  },
  unauthenticated: {
    title: "ログインが必要です",
    body: "この研究デスクは招待制です。ログイン状態を確認して、もう一度お試しください。",
  },
  unavailable: {
    title: "研究デスクに接続できません",
    body: "データベースまたは認証サービスを確認しています。しばらくしてから再度お試しください。",
  },
  unknown: {
    title: "読み込みに失敗しました",
    body: "状態を確認できませんでした。再読み込みしても解決しない場合は管理者へ連絡してください。",
  },
} as const;

function AccessPanel({
  state,
  errorMessage,
  onLogin,
  onRetry,
  hasPendingDeliveryToken = false,
}: {
  state: keyof typeof ACCESS_COPY;
  errorMessage?: string | null;
  onLogin?: () => void;
  onRetry?: () => void;
  hasPendingDeliveryToken?: boolean;
}) {
  const copy = ACCESS_COPY[state];
  return (
    <div className="mx-auto flex min-h-[70dvh] w-full max-w-3xl items-center px-4 py-12 md:px-8">
      <section className="w-full border-t-2 border-[#1f6090] bg-[#fffdf8] p-6 shadow-[0_18px_45px_rgba(24,38,58,0.08)] dark:border-sky-500 dark:bg-slate-900 md:p-9">
        <BookOpenCheck strokeWidth={ICON_STROKE} className="h-8 w-8 text-[#1f6090] dark:text-sky-400" />
        <h1 className="market-morning-serif mt-6 text-3xl font-semibold tracking-[-0.04em] text-[#18263a] dark:text-slate-100 md:text-4xl">
          {copy.title}
        </h1>
        <p className="mt-4 max-w-[58ch] text-sm leading-7 text-[#5f7080] dark:text-slate-300">
          {copy.body}
        </p>
        {errorMessage ? (
          <p className="mt-4 border-l-2 border-[#ad4d47] pl-3 text-xs text-[#8f4742] dark:text-rose-300">
            {errorMessage}
          </p>
        ) : null}
        {state === "unauthenticated" && hasPendingDeliveryToken ? (
          <p className="mt-4 text-xs leading-6 text-[#647487] dark:text-slate-400">
            ログイン画面へ移動した場合は、ログイン後にメールの専用リンクをもう一度開いてください。
          </p>
        ) : null}
        <div className="mt-7 flex flex-wrap gap-3">
          {onLogin ? (
            <button
              type="button"
              onClick={onLogin}
              className="inline-flex items-center gap-2 whitespace-nowrap bg-[#1f6090] px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-[#184f78] active:translate-y-px dark:bg-sky-600 dark:hover:bg-sky-500"
            >
              <LogIn strokeWidth={ICON_STROKE} className="h-4 w-4" />
              ログイン
            </button>
          ) : null}
          {onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="inline-flex items-center gap-2 whitespace-nowrap border border-[#c9c4b8] px-4 py-2.5 text-sm font-semibold text-[#33465a] transition hover:bg-[#eee8dc] active:translate-y-px dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
            >
              <RefreshCw strokeWidth={ICON_STROKE} className="h-4 w-4" />
              状態を再確認
            </button>
          ) : null}
          <Link
            to="/"
            className="inline-flex items-center gap-2 whitespace-nowrap border border-[#c9c4b8] px-4 py-2.5 text-sm font-medium text-[#33465a] transition hover:bg-[#eee8dc] active:translate-y-px dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
          >
            <ArrowLeft strokeWidth={ICON_STROKE} className="h-4 w-4" />
            Vibe-Trading に戻る
          </Link>
        </div>
      </section>
    </div>
  );
}

function AccessState({ hasPendingDeliveryToken }: { hasPendingDeliveryToken: boolean }) {
  const { accessState, errorMessage, reload } = useMarketMorning();
  const [loginError, setLoginError] = useState<string | null>(null);
  const canLogin =
    accessState === "unauthenticated" &&
    hasMarketMorningAuthLifecycleAdapter("product");
  const login = useCallback(async () => {
    setLoginError(null);
    try {
      await beginMarketMorningLogin(
        "product",
        `${window.location.pathname}${window.location.search}`,
      );
      await reload();
    } catch {
      setLoginError("ログインを開始できませんでした。もう一度お試しください。");
    }
  }, [reload]);
  return (
    <AccessPanel
      state={accessState ?? "unknown"}
      errorMessage={loginError ?? errorMessage}
      onLogin={canLogin ? () => void login() : undefined}
      onRetry={() => void reload()}
      hasPendingDeliveryToken={hasPendingDeliveryToken}
    />
  );
}

function DeliveryLinkRedemption({ token }: { token: string | null }) {
  const { status } = useMarketMorning();
  const attempted = useRef(false);

  useEffect(() => {
    if (status !== "ready" || token === null || attempted.current) return;
    attempted.current = true;
    void marketMorningApi.redeemDeliveryLink(token).catch(() => undefined);
  }, [status, token]);

  return null;
}

function ProductFrame({ pendingDeliveryToken }: { pendingDeliveryToken: string | null }) {
  const { status, reload } = useMarketMorning();
  const { dark, toggle } = useDarkMode();
  const [logoutError, setLogoutError] = useState(false);
  const [signedOut, setSignedOut] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const canLogout = hasMarketMorningAuthLifecycleAdapter("product");
  const logout = useCallback(async () => {
    setLogoutError(false);
    try {
      await endMarketMorningSession("product", "/");
      setSignedOut(true);
    } catch {
      setLogoutError(true);
    }
  }, []);
  const login = useCallback(async () => {
    setLoginError(null);
    try {
      await beginMarketMorningLogin("product", "/market-morning");
      await reload();
      setSignedOut(false);
    } catch {
      setLoginError("ログインを開始できませんでした。もう一度お試しください。");
    }
  }, [reload]);

  if (signedOut) {
    return (
      <div className="min-h-[100dvh] bg-[#f6f3eb] dark:bg-slate-950">
        <AccessPanel
          state="unauthenticated"
          errorMessage={loginError}
          onLogin={() => void login()}
        />
      </div>
    );
  }

  return (
    <div className="min-h-[100dvh] bg-[#f6f3eb] text-[#18263a] dark:bg-slate-950 dark:text-slate-100 md:grid md:grid-cols-[230px_minmax(0,1fr)]">
      <aside className="bg-[#15253a] text-slate-100 md:sticky md:top-0 md:flex md:min-h-[100dvh] md:flex-col md:px-4 md:py-6">
        <div className="flex items-center justify-between gap-4 px-4 py-4 md:block md:px-2 md:py-0">
          <ProductMark />
          <button
            type="button"
            onClick={toggle}
            className="grid h-9 w-9 place-items-center border border-white/15 text-[#b8c8d1] transition hover:bg-white/5 hover:text-white md:hidden"
            aria-label={dark ? "ライトモード" : "ダークモード"}
          >
            {dark ? <Sun strokeWidth={ICON_STROKE} className="h-4 w-4" /> : <Moon strokeWidth={ICON_STROKE} className="h-4 w-4" />}
          </button>
        </div>

        <nav className="flex gap-1 overflow-x-auto border-t border-white/10 px-3 py-3 md:mt-10 md:grid md:border-0 md:px-0 md:py-0" aria-label="Market Morning">
          {NAV_ITEMS.map(({ to, end, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex shrink-0 items-center gap-2 px-3 py-2.5 text-xs transition active:translate-y-px md:w-full",
                  isActive
                    ? "bg-[#29465d] font-semibold text-white shadow-[inset_2px_0_0_#8ecbd1]"
                    : "text-[#adbdc9] hover:bg-white/5 hover:text-white",
                )
              }
            >
              <Icon strokeWidth={ICON_STROKE} className="h-4 w-4" />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto hidden border-t border-white/10 pt-4 md:block">
          <p className="px-3 text-[10px] leading-5 text-[#8fa4b1]">
            売買を促さず、確認すべき事実と一次情報を整理します。
          </p>
          <button
            type="button"
            onClick={toggle}
            className="mt-3 flex w-full items-center gap-2 px-3 py-2 text-xs text-[#adbdc9] transition hover:bg-white/5 hover:text-white"
          >
            {dark ? <Sun strokeWidth={ICON_STROKE} className="h-4 w-4" /> : <Moon strokeWidth={ICON_STROKE} className="h-4 w-4" />}
            {dark ? "ライトモード" : "ダークモード"}
          </button>
          <Link to="/" className="mt-1 flex items-center gap-2 px-3 py-2 text-xs text-[#adbdc9] transition hover:bg-white/5 hover:text-white">
            <ArrowLeft strokeWidth={ICON_STROKE} className="h-4 w-4" />
            Vibe-Trading
          </Link>
        </div>
      </aside>

      <div className="min-w-0">
        <header className="flex min-h-16 items-center justify-between border-b border-[#d9d4c8] px-4 md:px-8 dark:border-slate-800">
          <div>
            <p className="text-xs font-medium text-[#526578] dark:text-slate-300">日本株リサーチの朝刊</p>
            <p className="mt-0.5 font-mono text-[9px] tracking-[0.08em] text-[#7c8995] dark:text-slate-500">
              PRIVATE BETA ACCESS
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="border border-[#c9c4b8] px-2.5 py-1 font-mono text-[9px] text-[#647487] dark:border-slate-700 dark:text-slate-400">
              JST
            </span>
            {canLogout && status === "ready" ? (
              <button
                type="button"
                onClick={() => void logout()}
                className="inline-flex items-center gap-1.5 border border-[#c9c4b8] px-2.5 py-1 text-[10px] text-[#526578] transition hover:bg-[#eee8dc] dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
              >
                <LogOut strokeWidth={ICON_STROKE} className="h-3.5 w-3.5" />
                ログアウト
              </button>
            ) : null}
          </div>
        </header>

        {logoutError ? (
          <p role="alert" className="border-b border-[#d9d4c8] px-4 py-2 text-xs text-[#8f4742] dark:border-slate-800 dark:text-rose-300 md:px-8">
            ログアウトを開始できませんでした。もう一度お試しください。
          </p>
        ) : null}

        <main>
          {status === "loading" ? <LoadingState /> : null}
          {status === "access_error" || status === "error" ? (
            <AccessState hasPendingDeliveryToken={pendingDeliveryToken !== null} />
          ) : null}
          {status === "ready" ? <Outlet /> : null}
        </main>
        <DeliveryLinkRedemption token={pendingDeliveryToken} />
      </div>
    </div>
  );
}

function ProductAuthBoundary({ pendingDeliveryToken }: { pendingDeliveryToken: string | null }) {
  const configured = hasMarketMorningAuthLifecycleAdapter("product");
  const [attempt, setAttempt] = useState(0);
  const [status, setStatus] = useState<
    "loading" | "ready" | "unauthenticated" | "error"
  >(
    configured ? "loading" : "ready",
  );
  const [loginError, setLoginError] = useState<string | null>(null);

  useEffect(() => {
    if (!configured) {
      setStatus("ready");
      return;
    }
    let active = true;
    setStatus("loading");
    void initializeMarketMorningAuth("product")
      .then(() => marketMorningAuthHeaders("product"))
      .then((headers) => {
        if (active) {
          setStatus(headers.Authorization ? "ready" : "unauthenticated");
        }
      })
      .catch(() => {
        if (active) setStatus("error");
      });
    return () => {
      active = false;
    };
  }, [attempt, configured]);

  const login = useCallback(async () => {
    setLoginError(null);
    try {
      await beginMarketMorningLogin(
        "product",
        `${window.location.pathname}${window.location.search}`,
      );
      setAttempt((value) => value + 1);
    } catch {
      setLoginError("ログインを開始できませんでした。もう一度お試しください。");
    }
  }, []);

  if (status === "loading") {
    return (
      <div className="min-h-[100dvh] bg-[#f6f3eb] dark:bg-slate-950">
        <LoadingState />
      </div>
    );
  }
  if (status === "unauthenticated") {
    return (
      <div className="min-h-[100dvh] bg-[#f6f3eb] dark:bg-slate-950">
        <AccessPanel
          state="unauthenticated"
          errorMessage={loginError}
          onLogin={() => void login()}
          onRetry={() => setAttempt((value) => value + 1)}
          hasPendingDeliveryToken={pendingDeliveryToken !== null}
        />
      </div>
    );
  }
  if (status === "error") {
    return (
      <div className="min-h-[100dvh] bg-[#f6f3eb] dark:bg-slate-950">
        <AccessPanel
          state="unavailable"
          errorMessage="ログイン状態を初期化できませんでした。"
          onRetry={() => setAttempt((value) => value + 1)}
        />
      </div>
    );
  }
  return (
    <MarketMorningProvider>
      <ProductFrame pendingDeliveryToken={pendingDeliveryToken} />
    </MarketMorningProvider>
  );
}

export function MarketMorningShell() {
  const enabled = isMarketMorningUiEnabled();
  const [pendingDeliveryToken, setPendingDeliveryToken] = useState<
    string | null | undefined
  >(undefined);

  useEffect(() => {
    if (!enabled) {
      setPendingDeliveryToken(null);
      return;
    }
    setPendingDeliveryToken((current) =>
      current === undefined ? consumeMarketMorningDeliveryToken() : current,
    );
  }, [enabled]);

  useEffect(() => {
    const existing = document.querySelector<HTMLMetaElement>('meta[name="robots"]');
    const previousContent = existing?.content;
    const meta = existing ?? document.createElement("meta");
    if (!existing) {
      meta.name = "robots";
      document.head.append(meta);
    }
    meta.content = "noindex,nofollow,noarchive";
    return () => {
      if (existing && previousContent !== undefined) meta.content = previousContent;
      else meta.remove();
    };
  }, []);

  if (!enabled) {
    return (
      <div className="min-h-[100dvh] bg-[#f6f3eb] dark:bg-slate-950">
        <AccessPanel state="disabled" />
      </div>
    );
  }
  if (pendingDeliveryToken === undefined) {
    return (
      <div className="min-h-[100dvh] bg-[#f6f3eb] dark:bg-slate-950">
        <LoadingState />
      </div>
    );
  }
  return <ProductAuthBoundary pendingDeliveryToken={pendingDeliveryToken} />;
}
