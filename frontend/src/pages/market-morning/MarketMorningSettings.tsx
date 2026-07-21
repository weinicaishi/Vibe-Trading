import { useEffect, useState } from "react";
import { Bell, Check, Download, FileCheck2, ShieldAlert, Trash2, X } from "lucide-react";
import { MarketMorningApiError, marketMorningApi } from "@/lib/marketMorningApi";
import { useMarketMorning } from "@/pages/market-morning/MarketMorningContext";

const ICON_STROKE = 1.7;

function messageFor(error: unknown): string {
  if (error instanceof MarketMorningApiError) return error.message;
  return "処理に失敗しました。もう一度お試しください。";
}

function ConsentRow({
  title,
  accepted,
  version,
}: {
  title: string;
  accepted: boolean;
  version: string | null;
}) {
  return (
    <div className="grid gap-3 border-b border-[#d9d4c8] py-4 last:border-b-0 dark:border-slate-800 sm:grid-cols-[1fr_auto] sm:items-center">
      <div>
        <p className="text-sm font-semibold text-[#18263a] dark:text-slate-100">{title}</p>
        <p className="mt-1 text-xs text-[#697886] dark:text-slate-500">
          {version ? `確認バージョン: ${version}` : "正式な説明文のバージョン確定後に確認できます。"}
        </p>
      </div>
      <span className={`inline-flex w-fit items-center gap-1.5 px-2.5 py-1 text-xs font-medium ${accepted ? "bg-[#e2f0ec] text-[#15766d] dark:bg-emerald-950 dark:text-emerald-300" : "bg-[#eee8dc] text-[#756d5f] dark:bg-slate-800 dark:text-slate-400"}`}>
        {accepted ? <Check strokeWidth={ICON_STROKE} className="h-3.5 w-3.5" /> : null}
        {accepted ? "確認済み" : "未確認"}
      </span>
    </div>
  );
}

export function MarketMorningSettings() {
  const { settings, setSettings } = useMarketMorning();
  const [emailOptIn, setEmailOptIn] = useState(settings?.email_opt_in ?? false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [confirmDeletion, setConfirmDeletion] = useState(false);
  const [deletionStatus, setDeletionStatus] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportStatus, setExportStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEmailOptIn(settings?.email_opt_in ?? false);
  }, [settings?.email_opt_in]);

  if (!settings) return null;

  const save = async () => {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const updated = await marketMorningApi.updateSettings({ email_opt_in: emailOptIn });
      setSettings(updated);
      setSaved(true);
    } catch (nextError) {
      setError(messageFor(nextError));
    } finally {
      setSaving(false);
    }
  };

  const requestDeletion = async () => {
    setSaving(true);
    setError(null);
    try {
      const result = await marketMorningApi.requestAccountDeletion();
      setDeletionStatus(
        result.status === "already_requested"
          ? "削除依頼はすでに受け付けています。"
          : "削除依頼を受け付けました。処理前に本人確認を行います。",
      );
      setConfirmDeletion(false);
    } catch (nextError) {
      setError(messageFor(nextError));
    } finally {
      setSaving(false);
    }
  };

  const exportData = async () => {
    setExporting(true);
    setExportStatus(null);
    setError(null);
    try {
      const result = await marketMorningApi.exportAccountData();
      const blob = new Blob([JSON.stringify(result, null, 2)], {
        type: "application/json;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `market-morning-account-data-${result.generated_at.slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
      setExportStatus("データを書き出しました");
    } catch (nextError) {
      setError(messageFor(nextError));
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-8 md:px-8 md:py-10">
      <header className="border-b border-[#d9d4c8] pb-7 dark:border-slate-800">
        <p className="font-mono text-[10px] tracking-[0.08em] text-[#1f6090] dark:text-sky-400">SETTINGS</p>
        <h1 className="market-morning-serif mt-3 text-3xl font-semibold tracking-[-0.045em] text-[#18263a] dark:text-slate-100 md:text-4xl">
          通知とデータの扱い。
        </h1>
        <p className="mt-3 max-w-[62ch] text-sm leading-7 text-[#5e6f80] dark:text-slate-300">
          朝刊の通知、説明文の確認状態、アカウント削除依頼を管理します。
        </p>
      </header>

      {error ? (
        <div role="alert" className="mt-6 border-l-2 border-[#ad4d47] bg-[#f8e8e4] px-4 py-3 text-sm text-[#884540] dark:bg-rose-950/40 dark:text-rose-200">
          {error}
        </div>
      ) : null}

      <div className="mt-8 grid gap-6">
        <section className="border-t-2 border-[#18263a] bg-[#fffdf8] p-5 dark:border-slate-600 dark:bg-slate-900 md:p-7">
          <div className="flex items-start gap-3">
            <Bell strokeWidth={ICON_STROKE} className="mt-0.5 h-5 w-5 text-[#1f6090] dark:text-sky-400" />
            <div className="flex-1">
              <h2 className="text-base font-semibold text-[#18263a] dark:text-slate-100">朝刊のメール通知</h2>
              <p className="mt-1 text-xs leading-6 text-[#697886] dark:text-slate-500">
                メールには「本日の朝刊が準備できました」という通知だけを送り、内容はサイト内で表示します。
              </p>
            </div>
          </div>
          <label className="mt-5 flex cursor-pointer items-center justify-between gap-4 border border-[#d9d4c8] bg-[#f6f3eb] px-4 py-3 dark:border-slate-800 dark:bg-slate-950">
            <span className="text-sm font-medium text-[#33485b] dark:text-slate-200">メール通知を受け取る</span>
            <input
              type="checkbox"
              checked={emailOptIn}
              onChange={(event) => {
                setEmailOptIn(event.target.checked);
                setSaved(false);
              }}
              className="h-4 w-4 accent-[#1f6090]"
            />
          </label>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => void save()}
              disabled={saving || emailOptIn === settings.email_opt_in}
              className="whitespace-nowrap bg-[#1f6090] px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-[#184f78] active:translate-y-px disabled:cursor-not-allowed disabled:opacity-50 dark:bg-sky-600 dark:hover:bg-sky-500"
            >
              {saving ? "保存中" : "設定を保存"}
            </button>
            {saved ? <span className="inline-flex items-center gap-1.5 text-xs font-medium text-[#15766d] dark:text-emerald-300"><Check strokeWidth={ICON_STROKE} className="h-4 w-4" />保存しました</span> : null}
          </div>
        </section>

        <section className="border-t-2 border-[#18263a] bg-[#fffdf8] p-5 dark:border-slate-600 dark:bg-slate-900 md:p-7">
          <div className="flex items-start gap-3">
            <FileCheck2 strokeWidth={ICON_STROKE} className="mt-0.5 h-5 w-5 text-[#1f6090] dark:text-sky-400" />
            <div>
              <h2 className="text-base font-semibold text-[#18263a] dark:text-slate-100">説明文の確認状態</h2>
              <p className="mt-1 text-xs leading-6 text-[#697886] dark:text-slate-500">
                リスク説明とデータ利用説明は、文書バージョンごとに履歴を保存します。
              </p>
            </div>
          </div>
          <div className="mt-4">
            <ConsentRow title="投資判断とリスクに関する説明" accepted={settings.risk_disclosure.accepted} version={settings.risk_disclosure.consent_version} />
            <ConsentRow title="データ取得とAI処理に関する説明" accepted={settings.data_disclosure.accepted} version={settings.data_disclosure.consent_version} />
          </div>
          <p className="mt-4 border-l-2 border-[#9b6b23] bg-[#f7eedc] px-4 py-3 text-xs leading-6 text-[#715a2b] dark:border-amber-500 dark:bg-amber-950/30 dark:text-amber-200">
            正式な説明文とサーバー側の現行バージョンが確定するまで、この画面から新しい同意は記録しません。
          </p>
        </section>

        <section className="border-t-2 border-[#18263a] bg-[#fffdf8] p-5 dark:border-slate-600 dark:bg-slate-900 md:p-7">
          <div className="flex items-start gap-3">
            <Download strokeWidth={ICON_STROKE} className="mt-0.5 h-5 w-5 text-[#1f6090] dark:text-sky-400" />
            <div>
              <h2 className="text-base font-semibold text-[#18263a] dark:text-slate-100">アカウントデータの書き出し</h2>
              <p className="mt-1 text-xs leading-6 text-[#697886] dark:text-slate-500">
                注目銘柄、朝刊、閲覧状態、メモ、同意履歴など、ご自身に紐づくデータを JSON で保存します。内部トークンや配信事業者の識別子は含みません。
              </p>
            </div>
          </div>
          <div className="mt-5 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={() => void exportData()}
              disabled={exporting}
              className="inline-flex items-center gap-2 whitespace-nowrap border border-[#1f6090] px-4 py-2.5 text-sm font-semibold text-[#1f6090] transition hover:bg-[#e7f0f6] disabled:opacity-50 dark:border-sky-500 dark:text-sky-400 dark:hover:bg-sky-950"
            >
              <Download strokeWidth={ICON_STROKE} className="h-4 w-4" />
              {exporting ? "書き出し中" : "データを書き出す"}
            </button>
            {exportStatus ? (
              <span role="status" className="inline-flex items-center gap-1.5 text-xs font-medium text-[#15766d] dark:text-emerald-300">
                <Check strokeWidth={ICON_STROKE} className="h-4 w-4" />{exportStatus}
              </span>
            ) : null}
          </div>
        </section>

        <section className="border-t-2 border-[#ad4d47] bg-[#fffdf8] p-5 dark:border-rose-500 dark:bg-slate-900 md:p-7">
          <div className="flex items-start gap-3">
            <ShieldAlert strokeWidth={ICON_STROKE} className="mt-0.5 h-5 w-5 text-[#ad4d47] dark:text-rose-400" />
            <div>
              <h2 className="text-base font-semibold text-[#18263a] dark:text-slate-100">アカウントの削除依頼</h2>
              <p className="mt-1 text-xs leading-6 text-[#697886] dark:text-slate-500">
                依頼後に本人確認と処理状況の確認を行います。この操作だけで直ちにデータを削除することはありません。
              </p>
            </div>
          </div>
          {deletionStatus ? (
            <p role="status" className="mt-5 bg-[#e2f0ec] px-4 py-3 text-sm text-[#15766d] dark:bg-emerald-950 dark:text-emerald-300">{deletionStatus}</p>
          ) : confirmDeletion ? (
            <div className="mt-5 flex flex-wrap gap-2">
              <button type="button" onClick={() => void requestDeletion()} disabled={saving} className="inline-flex items-center gap-2 whitespace-nowrap bg-[#ad4d47] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">
                <Trash2 strokeWidth={ICON_STROKE} className="h-4 w-4" />削除を依頼する
              </button>
              <button type="button" onClick={() => setConfirmDeletion(false)} className="inline-flex items-center gap-2 whitespace-nowrap border border-[#c9c4b8] px-4 py-2.5 text-sm font-medium text-[#526578] dark:border-slate-700 dark:text-slate-300">
                <X strokeWidth={ICON_STROKE} className="h-4 w-4" />キャンセル
              </button>
            </div>
          ) : (
            <button type="button" onClick={() => setConfirmDeletion(true)} className="mt-5 inline-flex items-center gap-2 whitespace-nowrap border border-[#ad4d47] px-4 py-2.5 text-sm font-semibold text-[#ad4d47] transition hover:bg-[#f8e8e4] active:translate-y-px dark:border-rose-500 dark:text-rose-400 dark:hover:bg-rose-950">
              <Trash2 strokeWidth={ICON_STROKE} className="h-4 w-4" />削除手続きを確認
            </button>
          )}
        </section>
      </div>
    </div>
  );
}
