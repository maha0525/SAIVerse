'use client';

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw, Download, ArrowUpCircle, CheckCircle2, Trash2 } from 'lucide-react';
import styles from './AddonCatalogPanel.module.css';
import AddonInstallProgressDialog, { CatalogOperation } from './AddonInstallProgressDialog';
import AddonActionConfirmDialog from './AddonActionConfirmDialog';

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
    description: string;
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
    const [registry, setRegistry] = useState<RegistryResponse | null>(null);
    const [installedInfo, setInstalledInfo] = useState<Map<string, { version: string }>>(new Map());
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // 進捗ダイアログ / 確認ダイアログの状態
    const [confirmTarget, setConfirmTarget] = useState<{
        entry: RegistryAddonEntry;
        operation: CatalogOperation;
        state: RowState;
    } | null>(null);
    const [progressTarget, setProgressTarget] = useState<{
        entry: RegistryAddonEntry;
        operation: CatalogOperation;
        deleteData?: boolean;
    } | null>(null);

    const loadAll = useCallback(async (force = false) => {
        setLoading(true);
        setError(null);
        try {
            const [regRes, instRes] = await Promise.all([
                fetch(`/api/addon-catalog/registry${force ? '?force=true' : ''}`),
                fetch('/api/addon-catalog/installed'),
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
        } catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            setError(msg);
            console.error('[AddonCatalog] loadAll failed:', msg);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        loadAll(false);
    }, [loadAll]);

    const rows = useMemo(() => {
        if (!registry) return [];
        return registry.registry.addons.map((entry) => ({
            entry,
            state: deriveRowState(entry, installedInfo),
        }));
    }, [registry, installedInfo]);

    const handleAction = (entry: RegistryAddonEntry, state: RowState) => {
        let operation: CatalogOperation;
        if (state.kind === 'not_installed') operation = 'install';
        else if (state.kind === 'update_available') operation = 'update';
        else return; // installed_latest はアクションなし (uninstall は別ボタン)
        setConfirmTarget({ entry, operation, state });
    };

    const handleUninstall = (entry: RegistryAddonEntry, state: RowState) => {
        if (state.kind === 'not_installed') return;
        setConfirmTarget({ entry, operation: 'uninstall', state });
    };

    const handleConfirmProceed = (deleteData: boolean | undefined) => {
        if (!confirmTarget) return;
        setProgressTarget({
            entry: confirmTarget.entry,
            operation: confirmTarget.operation,
            deleteData,
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
                    <h3>カタログ</h3>
                    {registry?.registry.updated_at && (
                        <span className={styles.updatedAt}>
                            updated: {registry.registry.updated_at}
                        </span>
                    )}
                </div>
                <div className={styles.actions}>
                    <button
                        className={styles.btnSecondary}
                        onClick={() => loadAll(true)}
                        disabled={loading}
                    >
                        <RefreshCw size={14} /> 再読み込み
                    </button>
                </div>
            </div>

            {loading && !registry && <div className={styles.empty}>読み込み中...</div>}
            {error && (
                <div className={styles.errorBox}>
                    <p>カタログ取得に失敗しました</p>
                    <p className={styles.errorDetail}>{error}</p>
                </div>
            )}
            {!loading && registry && rows.length === 0 && (
                <div className={styles.empty}>カタログにアドオンが登録されていません</div>
            )}

            {rows.length > 0 && (
                <div className={styles.list}>
                    {rows.map(({ entry, state }) => (
                        <div key={entry.id} className={styles.row}>
                            <div className={styles.rowLeft}>
                                <div className={styles.rowName}>
                                    {entry.display_name}
                                    {state.kind === 'installed_latest' && (
                                        <span className={`${styles.badge} ${styles.badgeInstalled}`}>
                                            <CheckCircle2 size={11} /> 導入済み v{state.current_version}
                                        </span>
                                    )}
                                    {state.kind === 'update_available' && (
                                        <span className={`${styles.badge} ${styles.badgeUpdate}`}>
                                            <ArrowUpCircle size={11} /> 更新あり v{state.current_version} → v{state.new_version}
                                        </span>
                                    )}
                                    {entry.category && (
                                        <span className={styles.badge}>{entry.category}</span>
                                    )}
                                    {entry.requires.gpu === 'required' && (
                                        <span className={`${styles.badge} ${styles.badgeRequire}`}>GPU 必須</span>
                                    )}
                                    {entry.requires.disk_gb && entry.requires.disk_gb >= 1 && (
                                        <span className={styles.badge}>
                                            {entry.requires.disk_gb} GB
                                        </span>
                                    )}
                                </div>
                                <div className={styles.rowDesc}>{entry.description}</div>
                                <div className={styles.rowSub}>
                                    {entry.id} ・ latest v{entry.latest}
                                </div>
                            </div>
                            <div className={styles.rowActions}>
                                {state.kind === 'not_installed' && (
                                    <button
                                        className={styles.btnPrimary}
                                        onClick={() => handleAction(entry, state)}
                                    >
                                        <Download size={12} /> 導入
                                    </button>
                                )}
                                {state.kind === 'update_available' && (
                                    <>
                                        <button
                                            className={styles.btnPrimary}
                                            onClick={() => handleAction(entry, state)}
                                        >
                                            <ArrowUpCircle size={12} /> 更新
                                        </button>
                                        <button
                                            className={`${styles.iconBtn} ${styles.deleteBtn}`}
                                            onClick={() => handleUninstall(entry, state)}
                                        >
                                            <Trash2 size={12} /> 削除
                                        </button>
                                    </>
                                )}
                                {state.kind === 'installed_latest' && (
                                    <button
                                        className={`${styles.iconBtn} ${styles.deleteBtn}`}
                                        onClick={() => handleUninstall(entry, state)}
                                    >
                                        <Trash2 size={12} /> 削除
                                    </button>
                                )}
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {confirmTarget && (
                <AddonActionConfirmDialog
                    entry={confirmTarget.entry}
                    operation={confirmTarget.operation}
                    state={confirmTarget.state}
                    onCancel={() => setConfirmTarget(null)}
                    onProceed={handleConfirmProceed}
                />
            )}

            {progressTarget && (
                <AddonInstallProgressDialog
                    addonId={progressTarget.entry.id}
                    displayName={progressTarget.entry.display_name}
                    operation={progressTarget.operation}
                    deleteData={progressTarget.deleteData}
                    onClose={handleProgressClose}
                />
            )}
        </div>
    );
}
