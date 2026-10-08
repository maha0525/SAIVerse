"use client";

import { useRef, useState } from 'react';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import styles from './HistoryContinuation.module.css';

interface Props {
    oldestId?: string;
    hasMore: boolean;
    ready: boolean;
    loading: boolean;
    onLoad: (before: string) => Promise<void>;
}

/** A hidden-only page cannot trigger scrolling. Keep the raw oldest ID reachable. */
export default function HistoryContinuation({ oldestId, hasMore, ready, loading, onLoad }: Props) {
    useLocale();
    const pending = useRef(false);
    const [busy, setBusy] = useState(false);
    if (!hasMore || !oldestId) return null;
    const load = async () => {
        if (!ready || loading || pending.current) return;
        pending.current = true;
        setBusy(true);
        try {
            await onLoad(oldestId);
        } finally {
            pending.current = false;
            setBusy(false);
        }
    };
    return (
        <div className={styles.container}>
            <button type="button" disabled={!ready || loading || busy} onClick={() => void load()} data-i18n="movementNotices.loadOlder">
                {uiText('movementNotices.loadOlder')}
            </button>
        </div>
    );
}
