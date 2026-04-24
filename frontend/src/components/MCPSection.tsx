"use client";

import React, { useState, useEffect, useCallback } from 'react';
import {
    Plug,
    ChevronDown,
    ChevronRight,
    RefreshCw,
    Square,
    PlayCircle,
    AlertTriangle,
} from 'lucide-react';
import styles from './MCPSection.module.css';

// ---------------------------------------------------------------------------
// Types (mirroring the REST shape of /api/mcp/*)
// ---------------------------------------------------------------------------

interface MCPServerStatus {
    instance_key: string | null;
    name: string;
    qualified_server_name: string;
    scope: string;
    persona_id: string | null;
    transport: string | null;
    connected: boolean;
    tool_count: number;
    tools: string[];
    refcount: number;
    referenced_by: string[];
    addon_name: string | null;
    source_path: string | null;
    note?: string;
}

interface MCPFailure {
    instance_key: string;
    attempts: number;
    category: string | null;
    message: string | null;
    seconds_until_retry: number;
    in_backoff: boolean;
}

interface MCPSectionProps {
    /** If set, only entries tied to this addon are shown.
     *  When unset, all MCP state is shown (global manager view). */
    addonName?: string;
    /** Start collapsed. Default: true (opened by explicit user click). */
    defaultCollapsed?: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const CATEGORY_LABELS: Record<string, string> = {
    runtime_missing: '必要なランタイムが見つからない',
    missing_config: '必須の設定値が未設定',
    auth_failed: '認証失敗',
    command_error: '起動コマンドエラー',
    network: 'ネットワークエラー',
    process_crash: 'プロセス異常終了',
    unknown: '不明なエラー',
};

function formatCategory(category: string | null): string {
    if (!category) return '不明';
    return CATEGORY_LABELS[category] ?? category;
}

function extractQualifiedFromInstanceKey(instanceKey: string): string {
    if (instanceKey.endsWith(':global')) {
        return instanceKey.slice(0, -':global'.length);
    }
    const personaIdx = instanceKey.indexOf(':persona:');
    if (personaIdx >= 0) {
        return instanceKey.slice(0, personaIdx);
    }
    return instanceKey;
}

// ---------------------------------------------------------------------------
// MCPSection
// ---------------------------------------------------------------------------

export default function MCPSection({
    addonName,
    defaultCollapsed = true,
}: MCPSectionProps) {
    const [expanded, setExpanded] = useState(!defaultCollapsed);
    const [servers, setServers] = useState<MCPServerStatus[]>([]);
    const [failures, setFailures] = useState<MCPFailure[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [actionInProgress, setActionInProgress] = useState<string | null>(null);

    const fetchState = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const [serversResp, failuresResp] = await Promise.all([
                fetch('/api/mcp/servers'),
                fetch('/api/mcp/failures'),
            ]);
            if (!serversResp.ok) {
                throw new Error(`/api/mcp/servers ${serversResp.status}`);
            }
            if (!failuresResp.ok) {
                throw new Error(`/api/mcp/failures ${failuresResp.status}`);
            }
            const serversData: MCPServerStatus[] = await serversResp.json();
            const failuresData: MCPFailure[] = await failuresResp.json();
            setServers(Array.isArray(serversData) ? serversData : []);
            setFailures(Array.isArray(failuresData) ? failuresData : []);
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            setError(msg);
            console.error('[MCPSection] fetch failed:', msg);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        if (expanded) {
            void fetchState();
        }
    }, [expanded, fetchState]);

    const runAction = async (
        actionKey: string,
        doAction: () => Promise<Response>,
    ) => {
        setActionInProgress(actionKey);
        try {
            const resp = await doAction();
            if (!resp.ok) {
                const text = await resp.text();
                console.error('[MCPSection] action failed:', actionKey, resp.status, text);
                setError(`操作に失敗しました (${resp.status}): ${text}`);
            } else {
                setError(null);
            }
        } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            console.error('[MCPSection] action error:', actionKey, msg);
            setError(msg);
        } finally {
            setActionInProgress(null);
            await fetchState();
        }
    };

    const handleReconnect = (serverName: string) =>
        runAction(`reconnect:${serverName}`, () =>
            fetch(
                `/api/mcp/servers/${encodeURIComponent(serverName)}/reconnect`,
                { method: 'POST' },
            ),
        );

    const handleStop = async (instanceKey: string) => {
        if (!confirm(`インスタンス "${instanceKey}" を停止しますか？`)) return;
        await runAction(`stop:${instanceKey}`, () =>
            fetch(
                `/api/mcp/instances/stop?instance_key=${encodeURIComponent(instanceKey)}`,
                { method: 'POST' },
            ),
        );
    };

    const handleRetry = (instanceKey: string) =>
        runAction(`retry:${instanceKey}`, () =>
            fetch(
                `/api/mcp/instances/retry?instance_key=${encodeURIComponent(instanceKey)}`,
                { method: 'POST' },
            ),
        );

    // Filter to a single addon when addonName is provided
    const visibleServers = addonName
        ? servers.filter((s) => s.addon_name === addonName)
        : servers;

    const serverLookup = new Map(
        servers.map((s) => [s.qualified_server_name, s.addon_name]),
    );
    const visibleFailures = addonName
        ? failures.filter((f) => {
            const qualified = extractQualifiedFromInstanceKey(f.instance_key);
            return serverLookup.get(qualified) === addonName;
        })
        : failures;

    // In addon-scoped mode, hide the section entirely when nothing to show
    if (
        addonName &&
        !loading &&
        !error &&
        visibleServers.length === 0 &&
        visibleFailures.length === 0
    ) {
        return null;
    }

    const headerLabel = addonName
        ? 'このアドオンの MCP サーバー'
        : 'MCP サーバー管理';

    return (
        <div className={styles.section}>
            <button
                type="button"
                className={styles.sectionHeader}
                onClick={() => setExpanded(!expanded)}
            >
                {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                <Plug size={16} />
                <span className={styles.headerLabel}>{headerLabel}</span>
                {!expanded && visibleFailures.length > 0 && (
                    <span className={styles.failureBadge}>
                        <AlertTriangle size={12} />
                        {visibleFailures.length}
                    </span>
                )}
            </button>

            {expanded && (
                <div className={styles.sectionBody}>
                    {loading && <p className={styles.loadingText}>読み込み中...</p>}
                    {error && (
                        <p className={styles.errorText}>エラー: {error}</p>
                    )}

                    {!loading &&
                        !error &&
                        visibleServers.length === 0 &&
                        visibleFailures.length === 0 && (
                            <p className={styles.emptyText}>
                                MCP サーバーは設定されていません。
                            </p>
                        )}

                    {visibleFailures.length > 0 && (
                        <div className={styles.failureList}>
                            <h4 className={styles.subHeader}>起動失敗中</h4>
                            {visibleFailures.map((failure) => (
                                <div
                                    key={failure.instance_key}
                                    className={styles.failureCard}
                                >
                                    <div className={styles.failureHeader}>
                                        <AlertTriangle size={14} />
                                        <code className={styles.instanceKey}>
                                            {failure.instance_key}
                                        </code>
                                        <span className={styles.categoryBadge}>
                                            {formatCategory(failure.category)}
                                        </span>
                                    </div>
                                    {failure.message && (
                                        <p className={styles.failureMessage}>
                                            {failure.message}
                                        </p>
                                    )}
                                    <div className={styles.failureFooter}>
                                        <span>
                                            試行 {failure.attempts} 回
                                            {failure.in_backoff &&
                                                ` ・再試行まで ${Math.ceil(
                                                    failure.seconds_until_retry,
                                                )} 秒`}
                                        </span>
                                        <button
                                            type="button"
                                            className={styles.actionButton}
                                            onClick={() =>
                                                handleRetry(failure.instance_key)
                                            }
                                            disabled={
                                                actionInProgress ===
                                                `retry:${failure.instance_key}`
                                            }
                                        >
                                            <PlayCircle size={14} /> 即時リトライ
                                        </button>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}

                    {visibleServers.length > 0 && (
                        <div className={styles.serverList}>
                            {visibleServers.map((server) => {
                                const keyForReact =
                                    server.instance_key ?? `${server.name}:pending`;
                                return (
                                    <div
                                        key={keyForReact}
                                        className={styles.serverCard}
                                    >
                                        <div className={styles.serverHeader}>
                                            <span
                                                className={`${styles.statusDot} ${
                                                    server.connected
                                                        ? styles.connected
                                                        : styles.disconnected
                                                }`}
                                                title={
                                                    server.connected
                                                        ? '接続中'
                                                        : '未接続'
                                                }
                                            />
                                            <span className={styles.serverName}>
                                                {server.name}
                                            </span>
                                            <span className={styles.scopeBadge}>
                                                {server.scope}
                                            </span>
                                            {server.persona_id && (
                                                <span className={styles.personaTag}>
                                                    {server.persona_id}
                                                </span>
                                            )}
                                        </div>

                                        <div className={styles.serverMeta}>
                                            {server.instance_key && (
                                                <span>
                                                    instance:{' '}
                                                    <code>
                                                        {server.instance_key}
                                                    </code>
                                                </span>
                                            )}
                                            <span>
                                                transport: {server.transport ?? '-'}
                                            </span>
                                            <span>tools: {server.tool_count}</span>
                                            <span>
                                                refcount: {server.refcount}
                                            </span>
                                        </div>

                                        {server.referenced_by.length > 0 && (
                                            <div className={styles.referrers}>
                                                {server.referenced_by.map((r) => (
                                                    <span
                                                        key={r}
                                                        className={
                                                            styles.referrerTag
                                                        }
                                                    >
                                                        {r}
                                                    </span>
                                                ))}
                                            </div>
                                        )}

                                        {server.note && (
                                            <p className={styles.serverNote}>
                                                {server.note}
                                            </p>
                                        )}

                                        <div className={styles.serverActions}>
                                            <button
                                                type="button"
                                                className={styles.actionButton}
                                                onClick={() =>
                                                    handleReconnect(
                                                        server.qualified_server_name,
                                                    )
                                                }
                                                disabled={
                                                    actionInProgress ===
                                                    `reconnect:${server.qualified_server_name}`
                                                }
                                            >
                                                <RefreshCw size={14} /> 再接続
                                            </button>
                                            {server.instance_key && (
                                                <button
                                                    type="button"
                                                    className={styles.actionButton}
                                                    onClick={() =>
                                                        handleStop(
                                                            server.instance_key!,
                                                        )
                                                    }
                                                    disabled={
                                                        actionInProgress ===
                                                        `stop:${server.instance_key}`
                                                    }
                                                >
                                                    <Square size={14} /> 停止
                                                </button>
                                            )}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
