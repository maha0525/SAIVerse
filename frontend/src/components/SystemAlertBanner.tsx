"use client";
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import { useEffect, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp, X } from "lucide-react";
import styles from "./SystemAlertBanner.module.css";
// 確認待ちの記憶の行があるあいだサーバーが置く警告には、確定の画面を開くボタンを
// 付ける。この警告を閉じても、設定の「便利機能」の入口 (SystemNoticeReviewEntry)
// から同じ画面を開ける。どちらから確定しても NOTICE_REVIEW_CHANGED_EVENT が出て、
// バナーと入口の両方が警告を取り直す。
import SystemNoticeReviewModal, {
    NOTICE_REVIEW_ALERT_ID,
    NOTICE_REVIEW_CHANGED_EVENT,
} from "./SystemNoticeReviewModal";

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

interface Props {
    /**
     * 画面が裏側と繋がっているか (page.tsx の backendConnected)。警告は裏側の
     * 起動時に作られるので、画面が裏側より先に開いた・裏側が再起動したときは、
     * 繋がり直した時点で一度取り直す。
     */
    backendConnected?: boolean;
}

export default function SystemAlertBanner({ backendConnected = true }: Props) {
    useLocale();
    const [alerts, setAlerts] = useState<SystemAlert[]>([]);
    const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
    const [busyId, setBusyId] = useState<string | null>(null);
    // 閉じたお知らせの id。バナーは画面最上部のヘッダー (地図・人々などのボタン列) に
    // 覆い被さるので、読み終えたお知らせをユーザーが退かせる手段が要る。閉じるのは
    // この画面を開いている間だけで、次の読み込みでまだ残っていれば再び表示される
    // (知らせ続ける義務はサーバー側の警告の寿命が持つ)。
    const [dismissedIds, setDismissedIds] = useState<Set<string>>(new Set());
    const [reviewOpen, setReviewOpen] = useState(false);

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
        window.addEventListener(NOTICE_REVIEW_CHANGED_EVENT, fetchAlerts);
        return () => window.removeEventListener(NOTICE_REVIEW_CHANGED_EVENT, fetchAlerts);
    }, []);

    useEffect(() => {
        if (backendConnected) fetchAlerts();
    }, [backendConnected]);

    const announceReviewChanged = () => {
        window.dispatchEvent(new Event(NOTICE_REVIEW_CHANGED_EVENT));
    };

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

    const visibleAlerts = alerts.filter((a) => !dismissedIds.has(a.id));
    const reviewModal = reviewOpen ? (
        <SystemNoticeReviewModal
            onClose={() => setReviewOpen(false)}
            onChanged={announceReviewChanged}
        />
    ) : null;
    if (visibleAlerts.length === 0) return reviewModal;

    const dismiss = (id: string) => {
        setDismissedIds((prev) => new Set(prev).add(id));
    };

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
                {visibleAlerts.map((alert) => {
                    const expanded = expandedIds.has(alert.id);
                    const levelClass =
                        alert.level === "critical"
                            ? styles.critical
                            : alert.level === "warning"
                              ? styles.warning
                              : styles.info;
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
                                {alert.id === NOTICE_REVIEW_ALERT_ID && (
                                    <button data-i18n="components.SystemAlertBanner.openNoticeReview"
                                        type="button"
                                        className={styles.secondaryButton}
                                        onClick={() => setReviewOpen(true)}
                                    >
                                        {uiText("components.SystemAlertBanner.openNoticeReview")}
                                    </button>
                                )}
                                <button data-i18n="components.SystemAlertBanner.dismiss components.SystemAlertBanner.dismissLabel"
                                    type="button"
                                    className={styles.closeButton}
                                    onClick={() => dismiss(alert.id)}
                                    disabled={busyId === alert.id}
                                    title={uiText("components.SystemAlertBanner.dismiss")}
                                    aria-label={uiText("components.SystemAlertBanner.dismissLabel")}
                                >
                                    <X size={16} />
                                </button>
                            </div>
                            {expanded && (
                                <div className={styles.body}>
                                    <p className={styles.message}>{alert.message}</p>
                                    {/* details が空 ({}) の警告 (確認待ちの行の警告など) には欄を描かない */}
                                    {alert.details && Object.keys(alert.details).length > 0 && (
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
            {reviewModal}
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
        parse_error: uiText("components.SystemAlertBanner.text020"),
        reason: uiText("components.SystemAlertBanner.text023"),
        kind: uiText("components.SystemAlertBanner.text025"),
        missing: uiText("components.SystemAlertBanner.text026"),
        file_entries: uiText("components.SystemAlertBanner.text027"),
        imported_rows: uiText("components.SystemAlertBanner.text028"),
        live_rows: uiText("components.SystemAlertBanner.text029"),
        path: uiText("components.SystemAlertBanner.text030"),
    };
    return labels[key] || key;
}
