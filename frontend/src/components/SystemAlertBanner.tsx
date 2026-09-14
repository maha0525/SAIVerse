"use client";
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


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
    useLocale();
    const [alerts, setAlerts] = useState<SystemAlert[]>([]);
    const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
    const [quarantineModalOpen, setQuarantineModalOpen] = useState(false);
    const [busyId, setBusyId] = useState<string | null>(null);

    const fetchAlerts = async () => {
        try {
            const res = await apiFetch("/api/system/alerts");
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
            uiText("components.SystemAlertBanner.text001") +
            uiText("components.SystemAlertBanner.text002") +
            uiText("components.SystemAlertBanner.text003"),
        );
        if (!ok) return;
        setBusyId(alert.id);
        try {
            const res = await apiFetch(
                `/api/system/legacy-log/${encodeURIComponent(buildingId)}/archive`,
                { method: "POST" },
            );
            if (!res.ok) {
                window.alert(uiText("components.SystemAlertBanner.text004", { p1: res.status }));
                return;
            }
            await fetchAlerts();
        } catch {
            window.alert(uiText("components.SystemAlertBanner.text005"));
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
                                    <button data-i18n="components.SystemAlertBanner.text006"
                                        type="button"
                                        className={styles.actionButton}
                                        onClick={() => setQuarantineModalOpen(true)}
                                    >{uiText("components.SystemAlertBanner.text006")}</button>
                                )}
                                {isUnreadableLegacyLog && (
                                    <button data-i18n="components.SystemAlertBanner.text007 components.SystemAlertBanner.text008"
                                        type="button"
                                        className={styles.secondaryButton}
                                        onClick={() => archiveLegacyLog(alert)}
                                        disabled={busyId === alert.id}
                                    >
                                        {busyId === alert.id ? uiText("components.SystemAlertBanner.text007") : uiText("components.SystemAlertBanner.text008")}
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
    get not_imported() { return uiText("components.SystemAlertBanner.text009"); },
    get live_rows_only() { return uiText("components.SystemAlertBanner.text010"); },
    get partial() { return uiText("components.SystemAlertBanner.text011"); },
    get unreadable() { return uiText("components.SystemAlertBanner.text012"); },
    get check_failed() { return uiText("components.SystemAlertBanner.text013"); },
};

function formatValue(value: unknown, key?: string): string {
    if (key === "kind" && typeof value === "string") {
        return LEGACY_LOG_KIND_LABELS[value] || value;
    }
    if (Array.isArray(value)) {
        return value.length === 0 ? uiText("components.SystemAlertBanner.text014") : value.join("\n");
    }
    if (value === null || value === undefined) return uiText("components.SystemAlertBanner.text015");
    return String(value);
}

function formatKey(key: string): string {
    const labels: Record<string, string> = {
        building_id: uiText("components.SystemAlertBanner.text016"),
        backup_path: uiText("components.SystemAlertBanner.text017"),
        corrupted_path: uiText("components.SystemAlertBanner.text018"),
        original_path: uiText("components.SystemAlertBanner.text019"),
        parse_error: uiText("components.SystemAlertBanner.text020"),
        rescue_error: uiText("components.SystemAlertBanner.text021"),
        recovery_instructions: uiText("components.SystemAlertBanner.text022"),
        reason: uiText("components.SystemAlertBanner.text023"),
        available_backups: uiText("components.SystemAlertBanner.text024"),
        kind: uiText("components.SystemAlertBanner.text025"),
        missing: uiText("components.SystemAlertBanner.text026"),
        file_entries: uiText("components.SystemAlertBanner.text027"),
        imported_rows: uiText("components.SystemAlertBanner.text028"),
        live_rows: uiText("components.SystemAlertBanner.text029"),
        path: uiText("components.SystemAlertBanner.text030"),
    };
    return labels[key] || key;
}
