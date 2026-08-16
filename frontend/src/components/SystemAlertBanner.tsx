"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp } from "lucide-react";
import styles from "./SystemAlertBanner.module.css";
import QuarantineModal from "./QuarantineModal";

interface SystemAlert {
    id: string;
    level: "critical" | "warning" | "info";
    title: string;
    message: string;
    details?: Record<string, unknown>;
}

interface AlertResponse {
    alerts: SystemAlert[];
}

export default function SystemAlertBanner() {
    const [alerts, setAlerts] = useState<SystemAlert[]>([]);
    const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
    const [quarantineModalOpen, setQuarantineModalOpen] = useState(false);
    const [busyId, setBusyId] = useState<string | null>(null);

    const fetchAlerts = async () => {
        try {
            const res = await fetch("/api/system/alerts");
            if (!res.ok) return;
            const data: AlertResponse = await res.json();
            setAlerts(data.alerts || []);
            const criticalIds = (data.alerts || [])
                .filter((a) => a.level === "critical")
                .map((a) => a.id);
            setExpandedIds(new Set(criticalIds));
        } catch {
            // Silently ignore — backend may not be ready yet
        }
    };

    useEffect(() => {
        fetchAlerts();
    }, []);

    // 読めなくなった古い履歴ファイルを脇へ移す。ファイルが移っていること自体が
    // 「ユーザーが認識した」の記録になるので、確認済みフラグは持たない。
    const archiveLegacyLog = async (alert: SystemAlert) => {
        const buildingId = String((alert.details || {}).building_id ?? "");
        if (!buildingId) return;
        const ok = window.confirm(
            "読めなくなった古い履歴ファイルを、同じフォルダの中で名前を変えて脇へ移します。\n\n" +
            "ファイルは消しません。後で復元したくなったら元の名前に戻せます。\n" +
            "この警告は移した時点で消えます。",
        );
        if (!ok) return;
        setBusyId(alert.id);
        try {
            const res = await fetch(
                `/api/system/legacy-log/${encodeURIComponent(buildingId)}/archive`,
                { method: "POST" },
            );
            if (!res.ok) {
                window.alert(`移せませんでした（${res.status}）。詳しくはログを見てください。`);
                return;
            }
            await fetchAlerts();
        } catch {
            window.alert("移せませんでした。バックエンドに繋がっていない可能性があります。");
        } finally {
            setBusyId(null);
        }
    };

    if (alerts.length === 0) return null;

    const hasQuarantineAlerts = alerts.some((a) => a.id.startsWith("quarantine_"));

    const toggle = (id: string) => {
        setExpandedIds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    return (
        <>
            <div className={styles.banner}>
                {alerts.map((alert) => {
                    const expanded = expandedIds.has(alert.id);
                    const levelClass =
                        alert.level === "critical"
                            ? styles.critical
                            : alert.level === "warning"
                              ? styles.warning
                              : styles.info;
                    const isQuarantine = alert.id.startsWith("quarantine_");
                    const isUnreadableLegacyLog =
                        (alert.details || {}).kind === "unreadable";
                    return (
                        <div key={alert.id} className={`${styles.alert} ${levelClass}`}>
                            <div className={styles.headerRow}>
                                <button
                                    type="button"
                                    className={styles.header}
                                    onClick={() => toggle(alert.id)}
                                    aria-expanded={expanded}
                                >
                                    <AlertTriangle size={18} className={styles.icon} />
                                    <span className={styles.title}>{alert.title}</span>
                                    {expanded ? (
                                        <ChevronUp size={16} className={styles.chevron} />
                                    ) : (
                                        <ChevronDown size={16} className={styles.chevron} />
                                    )}
                                </button>
                                {isQuarantine && (
                                    <button
                                        type="button"
                                        className={styles.actionButton}
                                        onClick={() => setQuarantineModalOpen(true)}
                                    >
                                        対応する
                                    </button>
                                )}
                                {isUnreadableLegacyLog && (
                                    <button
                                        type="button"
                                        className={styles.secondaryButton}
                                        onClick={() => archiveLegacyLog(alert)}
                                        disabled={busyId === alert.id}
                                    >
                                        {busyId === alert.id ? "移しています…" : "ファイルを脇へ移す"}
                                    </button>
                                )}
                            </div>
                            {expanded && (
                                <div className={styles.body}>
                                    <p className={styles.message}>{alert.message}</p>
                                    {alert.details && (
                                        <dl className={styles.details}>
                                            {Object.entries(alert.details).map(([key, value]) => (
                                                <div key={key} className={styles.detailRow}>
                                                    <dt className={styles.detailKey}>{formatKey(key)}</dt>
                                                    <dd className={styles.detailValue}>{formatValue(value, key)}</dd>
                                                </div>
                                            ))}
                                        </dl>
                                    )}
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
            {hasQuarantineAlerts && (
                <QuarantineModal
                    isOpen={quarantineModalOpen}
                    onClose={() => setQuarantineModalOpen(false)}
                    onResolved={fetchAlerts}
                />
            )}
        </>
    );
}

// 検算が返す状態の名前。そのまま出すと画面に英語の内部用語が並ぶ。
const LEGACY_LOG_KIND_LABELS: Record<string, string> = {
    not_imported: "まだ移していない",
    live_rows_only: "新しい会話が先に入っている",
    partial: "一部だけ移せていない",
    unreadable: "ファイルが読めない",
    check_failed: "確認できなかった",
};

function formatValue(value: unknown, key?: string): string {
    if (key === "kind" && typeof value === "string") {
        return LEGACY_LOG_KIND_LABELS[value] || value;
    }
    if (Array.isArray(value)) {
        return value.length === 0 ? "(なし)" : value.join("\n");
    }
    if (value === null || value === undefined) return "(なし)";
    return String(value);
}

function formatKey(key: string): string {
    const labels: Record<string, string> = {
        building_id: "ビルディングID",
        backup_path: "退避先",
        corrupted_path: "退避先",
        original_path: "元の場所",
        parse_error: "パースエラー",
        rescue_error: "退避エラー",
        recovery_instructions: "復元手順",
        reason: "異常理由",
        available_backups: "利用可能なバックアップ",
        kind: "状態",
        missing: "移せていない件数",
        file_entries: "古いファイルの件数",
        imported_rows: "移し終わった件数",
        live_rows: "新しい会話の件数",
        path: "古いファイルの場所",
    };
    return labels[key] || key;
}
