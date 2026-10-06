'use client';
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { resolveI18nText } from '@/i18n/resolve';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw, Download, ArrowUpCircle, CheckCircle2, Trash2, Loader2 } from 'lucide-react';
import styles from './AddonCatalogPanel.module.css';
import AddonInstallProgressDialog, { CatalogOperation } from './AddonInstallProgressDialog';
import AddonActionConfirmDialog, { ConfirmProceedResult, SetupLoadState } from './AddonActionConfirmDialog';
import type { SetupAnswers, SetupQuestion, SetupStep } from './AddonSetupQuestions';

// ---------------------------------------------------------------------------
// Types (mirror backend pydantic models)
// ---------------------------------------------------------------------------

interface RegistryVersionEntry {
    version: string;
    commit: string;
    setup_version: number;
    min_saiverse_version?: string | null;
    released_at?: string | null;
    changelog_url?: string | null;
}

interface RegistryRequires {
    gpu?: string | null;
    disk_gb?: number | null;
    os?: string[];
}

interface RegistryAddonEntry {
    id: string;
    display_name: string;
    display_name_en?: string | null;
    display_name_i18n?: Record<string, string> | null;
    description: string;
    description_en?: string | null;
    description_i18n?: Record<string, string> | null;
    category?: string | null;
    repo_url: string;
    versions: RegistryVersionEntry[];
    latest: string;
    icon_url?: string | null;
    requires: RegistryRequires;
}

interface RegistryResponse {
    registry_url: string;
    registry: {
        schema_version: number;
        updated_at?: string | null;
        addons: RegistryAddonEntry[];
    };
}

/** POST /install/prepare・/update/prepare の答え (update だけ needs_setup を持つ) */
interface PrepareResponse {
    addon_id: string;
    version?: string;
    setup_version?: number;
    needs_setup?: boolean;
    questions?: SetupQuestion[];
    steps?: SetupStep[];
}

/** 失敗した応答から、利用者に見せる理由を取り出す (FastAPI の detail を優先) */
async function readErrorDetail(res: Response): Promise<string> {
    const text = await res.text().catch(() => '');
    try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed.detail === 'string') return parsed.detail;
    } catch {
        // JSON でなければ本文をそのまま使う
    }
    return `HTTP ${res.status}${text ? `: ${text}` : ''}`;
}

interface AddonCatalogPanelProps {
    /** 親モーダルへ「installed list 再 fetch して」と通知する callback */
    onInstalledChanged: () => Promise<void> | void;
}

// ---------------------------------------------------------------------------
// State derivation
// ---------------------------------------------------------------------------

type RowState =
    | { kind: 'not_installed' }
    | { kind: 'installed_latest'; current_version: string }
    | { kind: 'update_available'; current_version: string; new_version: string };

/**
 * セマンティックバージョンを数値タプルとして比較する。
 * `a > b` なら正、`a < b` なら負、等しければ 0 を返す。
 * 数値化できない部分 (プレリリースタグ等) は 0 とみなす。
 * バックエンド `api/routes/system.py:_compare_versions` と同じロジック。
 */
function compareVersions(a: string, b: string): number {
    const parse = (v: string): number[] =>
        v.replace(/^v/, '').split('.').map((p) => {
            const n = parseInt(p, 10);
            return Number.isNaN(n) ? 0 : n;
        });
    const pa = parse(a);
    const pb = parse(b);
    const len = Math.max(pa.length, pb.length);
    for (let i = 0; i < len; i++) {
        const diff = (pa[i] ?? 0) - (pb[i] ?? 0);
        if (diff !== 0) return diff > 0 ? 1 : -1;
    }
    return 0;
}

function deriveRowState(
    entry: RegistryAddonEntry,
    installedManifestByName: Map<string, { version: string }>,
): RowState {
    const installed = installedManifestByName.get(entry.id);
    if (!installed) return { kind: 'not_installed' };
    // latest が installed より真に新しいときだけ「更新あり」。
    // 等しい / installed が先行 (ローカル開発版など) は「導入済み」扱い。
    if (compareVersions(entry.latest, installed.version) > 0) {
        return {
            kind: 'update_available',
            current_version: installed.version,
            new_version: entry.latest,
        };
    }
    return { kind: 'installed_latest', current_version: installed.version };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function AddonCatalogPanel({
    onInstalledChanged,
}: AddonCatalogPanelProps) {
    const currentLocale = useLocale();
    const [registry, setRegistry] = useState<RegistryResponse | null>(null);
    const [installedInfo, setInstalledInfo] = useState<Map<string, { version: string }>>(new Map());
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // 進捗ダイアログ / 確認ダイアログの状態
    const [confirmTarget, setConfirmTarget] = useState<{
        entry: RegistryAddonEntry;
        operation: CatalogOperation;
        state: RowState;
        /** install / update の prepare の結果。uninstall では undefined */
        setup?: SetupLoadState;
    } | null>(null);
    const [progressTarget, setProgressTarget] = useState<{
        entry: RegistryAddonEntry;
        operation: CatalogOperation;
        deleteData?: boolean;
        answers?: SetupAnswers;
    } | null>(null);

    // prepare の呼び出しごとに番号を振る。応答が返ったときに番号が進んで
    // いれば、その確認ダイアログはもう閉じられている。
    const prepareSeqRef = useRef(0);
    // install/prepare が成功し、まだ confirm も cancel もしていない addon_id。
    // 確認ダイアログを閉じる経路 (キャンセル・背景クリック・モーダルごと
    // 閉じる) のどれでも、ここが残っていれば cancel を送る。
    const pendingInstallRef = useRef<string | null>(null);
    // サーバーで操作 (導入・更新・削除・選択肢の追加) が走っている addon_id。
    // 進捗の小窓を途中で閉じても処理は裏で続くので、その間は行の操作を止める
    // (押してもサーバーの鍵で断られるだけ)。終わったら一覧を取り直す。
    const [runningOps, setRunningOps] = useState<Set<string>>(new Set());

    const fetchRunningOps = useCallback(async (): Promise<Set<string>> => {
        const res = await apiFetch('/api/addon-catalog/operations');
        if (!res.ok) throw new Error(await readErrorDetail(res));
        const ops: Array<{ addon_id: string }> = await res.json();
        return new Set(ops.map((op) => op.addon_id));
    }, []);

    const sendInstallCancel = useCallback((addonId: string) => {
        apiFetch('/api/addon-catalog/install/cancel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ addon_id: addonId }),
        }).then(async (res) => {
            if (!res.ok) {
                console.error('[AddonCatalog] install/cancel failed:', await readErrorDetail(res));
            }
        }).catch((e) => {
            console.error('[AddonCatalog] install/cancel failed:', e instanceof Error ? e.message : String(e));
        });
    }, []);

    // パネルが消えるとき (アドオン管理モーダルを閉じた / タブを切り替えた)、
    // 開きかけの導入を取り消す。応答待ちの prepare も番号を進めて取り消し扱いにする。
    useEffect(() => () => {
        prepareSeqRef.current++;
        const pending = pendingInstallRef.current;
        pendingInstallRef.current = null;
        if (pending) sendInstallCancel(pending);
    }, [sendInstallCancel]);

    const startPrepare = async (entry: RegistryAddonEntry, operation: 'install' | 'update') => {
        const seq = ++prepareSeqRef.current;
        const path = operation === 'install'
            ? '/api/addon-catalog/install/prepare'
            : '/api/addon-catalog/update/prepare';
        let setup: SetupLoadState;
        try {
            const res = await apiFetch(path, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ addon_id: entry.id }),
            });
            if (!res.ok) throw new Error(await readErrorDetail(res));
            const data: PrepareResponse = await res.json();
            if (seq !== prepareSeqRef.current) {
                // 応答を待つ間に確認ダイアログが閉じられた。導入の準備は取り消す。
                // ただし同じアドオンの新しい準備が生きているとき (閉じてすぐ開き直した
                // とき) は送らない — cancel は addon_id でしか区別されず、新しい方の
                // 準備したフォルダを消してしまう。
                if (operation === 'install' && pendingInstallRef.current !== entry.id) {
                    sendInstallCancel(entry.id);
                }
                return;
            }
            if (operation === 'install') pendingInstallRef.current = entry.id;
            setup = {
                status: 'ready',
                plan: { questions: data.questions ?? [], steps: data.steps ?? [] },
                // install は常に setup が走る。update はサーバーの判定に従う
                needsSetup: operation === 'install' ? true : data.needs_setup !== false,
            };
        } catch (e) {
            if (seq !== prepareSeqRef.current) return;
            const msg = e instanceof Error ? e.message : String(e);
            console.error(`[AddonCatalog] ${operation}/prepare failed:`, msg);
            setup = { status: 'error', message: msg };
        }
        setConfirmTarget((prev) => (
            prev && prev.entry.id === entry.id && prev.operation === operation
                ? { ...prev, setup }
                : prev
        ));
    };

    const closeConfirm = () => {
        prepareSeqRef.current++;
        const pending = pendingInstallRef.current;
        pendingInstallRef.current = null;
        if (pending) sendInstallCancel(pending);
        setConfirmTarget(null);
    };

    const loadAll = useCallback(async (force = false) => {
        setLoading(true);
        setError(null);
        try {
            const [regRes, instRes] = await Promise.all([
                apiFetch(`/api/addon-catalog/registry${force ? '?force=true' : ''}`),
                apiFetch('/api/addon-catalog/installed'),
            ]);
            if (!regRes.ok) {
                const text = await regRes.text();
                throw new Error(`registry fetch failed: ${regRes.status} ${text}`);
            }
            if (!instRes.ok) {
                throw new Error(`installed fetch failed: ${instRes.status}`);
            }
            const reg: RegistryResponse = await regRes.json();
            const inst: Array<{ addon_id: string; version: string }> = await instRes.json();
            setRegistry(reg);
            const m = new Map<string, { version: string }>();
            for (const a of inst) m.set(a.addon_id, { version: a.version });
            setInstalledInfo(m);
            try {
                setRunningOps(await fetchRunningOps());
            } catch (e) {
                // 取れなくても一覧は出す (行の操作はサーバーの鍵が守る)
                console.error('[AddonCatalog] operations fetch failed:', e instanceof Error ? e.message : String(e));
            }
        } catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            setError(msg);
            console.error('[AddonCatalog] loadAll failed:', msg);
        } finally {
            setLoading(false);
        }
    }, [fetchRunningOps]);

    useEffect(() => {
        loadAll(false);
    }, [loadAll]);

    // 裏で走っている操作がある間は、終わるまで数秒おきに確かめる
    // (進捗の小窓を開いている間は小窓が見張るので、ここでは見ない)
    useEffect(() => {
        if (runningOps.size === 0 || progressTarget) return;
        let cancelled = false;
        const timer = setTimeout(async () => {
            let next: Set<string>;
            try {
                next = await fetchRunningOps();
            } catch {
                // 次の確認をもう一度予約する (中身は同じ、参照だけ変える)
                if (!cancelled) setRunningOps(new Set(runningOps));
                return;
            }
            if (cancelled) return;
            const ended = Array.from(runningOps).some((id) => !next.has(id));
            setRunningOps(next);
            if (ended) {
                await loadAll(false);
                await onInstalledChanged();
            }
        }, 5000);
        return () => {
            cancelled = true;
            clearTimeout(timer);
        };
    }, [runningOps, progressTarget, fetchRunningOps, loadAll, onInstalledChanged]);

    const rows = useMemo(() => {
        if (!registry) return [];
        return registry.registry.addons.map((entry) => ({
            entry,
            state: deriveRowState(entry, installedInfo),
        }));
    }, [registry, installedInfo]);

    const handleAction = (entry: RegistryAddonEntry, state: RowState) => {
        if (runningOps.has(entry.id)) return;
        let operation: CatalogOperation;
        if (state.kind === 'not_installed') operation = 'install';
        else if (state.kind === 'update_available') operation = 'update';
        else return; // installed_latest はアクションなし (uninstall は別ボタン)
        // 確認ダイアログはすぐ開き、質問は prepare の応答で埋める
        setConfirmTarget({ entry, operation, state, setup: { status: 'loading' } });
        void startPrepare(entry, operation);
    };

    const handleUninstall = (entry: RegistryAddonEntry, state: RowState) => {
        if (state.kind === 'not_installed') return;
        if (runningOps.has(entry.id)) return;
        setConfirmTarget({ entry, operation: 'uninstall', state });
    };

    const handleConfirmProceed = ({ deleteData, answers }: ConfirmProceedResult) => {
        if (!confirmTarget) return;
        // confirm に進むので、準備した導入は取り消さない
        prepareSeqRef.current++;
        pendingInstallRef.current = null;
        setProgressTarget({
            entry: confirmTarget.entry,
            operation: confirmTarget.operation,
            deleteData,
            answers,
        });
        setConfirmTarget(null);
    };

    const handleProgressClose = async () => {
        setProgressTarget(null);
        await loadAll(false);
        await onInstalledChanged();
    };

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <div className={styles.headerLeft}>
                    <h3 data-i18n="components.AddonCatalogPanel.text001">{uiText("components.AddonCatalogPanel.text001")}</h3>
                    {registry?.registry.updated_at && (
                        <span className={styles.updatedAt}>
                            {uiText("components.AddonCatalogPanel.label001")}{registry.registry.updated_at}
                        </span>
                    )}
                </div>
                <div className={styles.actions}>
                    <button data-i18n="components.AddonCatalogPanel.text002"
                        className={styles.btnSecondary}
                        onClick={() => loadAll(true)}
                        disabled={loading}
                    >
                        <RefreshCw size={14} />{uiText("components.AddonCatalogPanel.text002")}</button>
                </div>
            </div>

            {loading && !registry && <div data-i18n="components.AddonCatalogPanel.text003" className={styles.empty}>{uiText("components.AddonCatalogPanel.text003")}</div>}
            {error && (
                <div className={styles.errorBox}>
                    <p data-i18n="components.AddonCatalogPanel.text004">{uiText("components.AddonCatalogPanel.text004")}</p>
                    <p className={styles.errorDetail}>{error}</p>
                </div>
            )}
            {!loading && registry && rows.length === 0 && (
                <div data-i18n="components.AddonCatalogPanel.text005" className={styles.empty}>{uiText("components.AddonCatalogPanel.text005")}</div>
            )}

            {rows.length > 0 && (
                <div className={styles.list}>
                    {rows.map(({ entry, state }) => {
                        const dispName = resolveI18nText(entry.display_name_i18n, currentLocale, entry.display_name_en, entry.display_name);
                        const descText = resolveI18nText(entry.description_i18n, currentLocale, entry.description_en, entry.description);
                        const busy = runningOps.has(entry.id);
                        return (
                            <div key={entry.id} className={styles.row}>
                                <div className={styles.rowLeft}>
                                    <div className={styles.rowName}>
                                        {dispName}
                                        {state.kind === 'installed_latest' && (
                                            <span data-i18n="components.AddonCatalogPanel.text006" className={`${styles.badge} ${styles.badgeInstalled}`}>
                                                <CheckCircle2 size={11} />{uiText("components.AddonCatalogPanel.text006")}{state.current_version}
                                            </span>
                                        )}
                                        {state.kind === 'update_available' && (
                                            <span data-i18n="components.AddonCatalogPanel.text007" className={`${styles.badge} ${styles.badgeUpdate}`}>
                                                <ArrowUpCircle size={11} />{uiText("components.AddonCatalogPanel.text007")}{state.current_version} → v{state.new_version}
                                            </span>
                                        )}
                                        {busy && (
                                            <span data-i18n="components.AddonCatalogPanel.text014" className={styles.badge}>
                                                <Loader2 size={11} />{uiText("components.AddonCatalogPanel.text014")}
                                            </span>
                                        )}
                                        {entry.category && (
                                            <span className={styles.badge}>{entry.category}</span>
                                        )}
                                        {entry.requires.gpu === 'required' && (
                                            <span data-i18n="components.AddonCatalogPanel.text008" className={`${styles.badge} ${styles.badgeRequire}`}>{uiText("components.AddonCatalogPanel.text008")}</span>
                                        )}
                                        {entry.requires.disk_gb && entry.requires.disk_gb >= 1 && (
                                            <span className={styles.badge}>
                                                {entry.requires.disk_gb} {uiText("components.AddonCatalogPanel.label002")}</span>
                                        )}
                                    </div>
                                    <div className={styles.rowDesc}>{descText}</div>
                                    <div data-i18n="components.AddonCatalogPanel.text009" className={styles.rowSub}>
                                        {entry.id}{uiText("components.AddonCatalogPanel.text009")}{entry.latest}
                                    </div>
                                </div>
                                <div className={styles.rowActions}>
                                    {state.kind === 'not_installed' && (
                                        <button data-i18n="components.AddonCatalogPanel.text010"
                                            className={styles.btnPrimary}
                                            onClick={() => handleAction(entry, state)}
                                            disabled={busy}
                                        >
                                            <Download size={12} />{uiText("components.AddonCatalogPanel.text010")}</button>
                                    )}
                                    {state.kind === 'update_available' && (
                                        <>
                                            <button data-i18n="components.AddonCatalogPanel.text011"
                                                className={styles.btnPrimary}
                                                onClick={() => handleAction(entry, state)}
                                                disabled={busy}
                                            >
                                                <ArrowUpCircle size={12} />{uiText("components.AddonCatalogPanel.text011")}</button>
                                            <button data-i18n="components.AddonCatalogPanel.text012"
                                                className={`${styles.iconBtn} ${styles.deleteBtn}`}
                                                onClick={() => handleUninstall(entry, state)}
                                                disabled={busy}
                                            >
                                                <Trash2 size={12} />{uiText("components.AddonCatalogPanel.text012")}</button>
                                        </>
                                    )}
                                    {state.kind === 'installed_latest' && (
                                        <button data-i18n="components.AddonCatalogPanel.text013"
                                            className={`${styles.iconBtn} ${styles.deleteBtn}`}
                                            onClick={() => handleUninstall(entry, state)}
                                            disabled={busy}
                                        >
                                            <Trash2 size={12} />{uiText("components.AddonCatalogPanel.text013")}</button>
                                    )}
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}

            {confirmTarget && (
                <AddonActionConfirmDialog
                    entry={confirmTarget.entry}
                    operation={confirmTarget.operation}
                    state={confirmTarget.state}
                    setup={confirmTarget.setup}
                    onCancel={closeConfirm}
                    onProceed={handleConfirmProceed}
                />
            )}

            {progressTarget && (
                <AddonInstallProgressDialog
                    addonId={progressTarget.entry.id}
                    displayName={progressTarget.entry.display_name}
                    operation={progressTarget.operation}
                    deleteData={progressTarget.deleteData}
                    answers={progressTarget.answers}
                    onClose={handleProgressClose}
                />
            )}
        </div>
    );
}
