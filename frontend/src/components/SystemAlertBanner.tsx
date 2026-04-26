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
                            </div>
                            {expanded && (
                                <div className={styles.body}>
                                    <p className={styles.message}>{alert.message}</p>
                                    {alert.details && (
                                        <dl className={styles.details}>
                                            {Object.entries(alert.details).map(([key, value]) => (
                                                <div key={key} className={styles.detailRow}>
                                                    <dt className={styles.detailKey}>{formatKey(key)}</dt>
                                                    <dd className={styles.detailValue}>{formatValue(value)}</dd>
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

function formatValue(value: unknown): string {
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
    };
    return labels[key] || key;
}
