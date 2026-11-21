"use client";
import { getFormatLocale, t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';

import { AlertCircle, AlertTriangle, X } from 'lucide-react';
import type { RuntimeAlert } from '@/hooks/useAddonEvents';
import styles from './RuntimeAlertCards.module.css';

interface Props {
    alerts: RuntimeAlert[];
    onDismiss: (id: number) => void;
}

function formatLastAt(epochSeconds: number): string {
    try {
        return new Intl.DateTimeFormat(getFormatLocale(), {
            month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit',
        }).format(new Date(epochSeconds * 1000));
    } catch {
        return new Date(epochSeconds * 1000).toLocaleString();
    }
}

/**
 * 動作中に見つけた、部屋に結びつかない警告のカード (docs/intent/runtime_alerts.md)。
 *
 * page.tsx の右下の通知の表示エリア (`toastContainer`) に、数秒で消える知らせと
 * 並べて置く。時間では消えず、閉じるボタンでだけ消える。同じ原因で何度も起きた
 * 知らせは一枚のまま、回数と最後に起きた時刻を出す。新しく起きたものが上。
 */
export default function RuntimeAlertCards({ alerts, onDismiss }: Props) {
    useLocale();
    if (alerts.length === 0) return null;
    const sorted = [...alerts].sort((a, b) => (b.last_at - a.last_at) || (b.id - a.id));
    return (
        <div className={styles.list}>
            {sorted.map(alert => {
                const isError = alert.level === 'error';
                const Icon = isError ? AlertCircle : AlertTriangle;
                return (
                    <div
                        key={alert.id}
                        className={`${styles.card} ${isError ? styles.error : styles.warning}`}
                        role="alert"
                    >
                        <Icon size={16} className={styles.icon} />
                        <div className={styles.body}>
                            <div className={styles.title}>{alert.title}</div>
                            <div className={styles.message}>{alert.message}</div>
                            {alert.count > 1 && (
                                <div className={styles.meta}>
                                    {uiText('components.RuntimeAlertCards.occurrence', {
                                        count: alert.count,
                                        time: formatLastAt(alert.last_at),
                                    })}
                                </div>
                            )}
                        </div>
                        <button
                            type="button"
                            className={styles.close}
                            onClick={() => onDismiss(alert.id)}
                            aria-label={uiText('components.RuntimeAlertCards.close')}
                            title={uiText('components.RuntimeAlertCards.close')}
                        >
                            <X size={14} />
                        </button>
                    </div>
                );
            })}
        </div>
    );
}
