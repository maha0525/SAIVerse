'use client';
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';

import React, { useState, useEffect } from 'react';
import { Loader2, Trash2, Brain, RefreshCw, XCircle } from 'lucide-react';
import styles from './WorkingMemoryViewer.module.css';

interface RecalledIdItem {
    type: string;
    id: string;
    title: string;
    uri: string;
    recalled_at: number | null;
}

interface WorkingMemoryViewerProps {
    personaId: string;
}

function formatTimestamp(ts: number): string {
    return new Date(ts * 1000).toLocaleString(getFormatLocale(), {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit',
    });
}

export default function WorkingMemoryViewer({ personaId }: WorkingMemoryViewerProps) {
    useLocale();
    const [items, setItems] = useState<RecalledIdItem[]>([]);
    const [maxCapacity, setMaxCapacity] = useState(10);
    const [isLoading, setIsLoading] = useState(false);
    const [isClearing, setIsClearing] = useState(false);
    const [removingId, setRemovingId] = useState<string | null>(null);

    const fetchItems = async () => {
        setIsLoading(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/working-memory`);
            if (res.ok) {
                const data = await res.json();
                setItems(data.recalled_ids);
                setMaxCapacity(data.max_capacity);
            }
        } catch (e) {
            console.error('Failed to fetch working memory:', e);
        } finally {
            setIsLoading(false);
        }
    };

    useEffect(() => {
        fetchItems();
    }, [personaId]);

    const removeItem = async (sourceId: string) => {
        setRemovingId(sourceId);
        try {
            const res = await apiFetch(
                `/api/people/${personaId}/working-memory/recall/${encodeURIComponent(sourceId)}`,
                { method: 'DELETE' },
            );
            if (res.ok) {
                await fetchItems();
            }
        } catch (e) {
            console.error('Failed to remove recalled ID:', e);
        } finally {
            setRemovingId(null);
        }
    };

    const clearAll = async () => {
        if (items.length === 0) return;
        setIsClearing(true);
        try {
            const res = await apiFetch(
                `/api/people/${personaId}/working-memory/recall`,
                { method: 'DELETE' },
            );
            if (res.ok) {
                await fetchItems();
            }
        } catch (e) {
            console.error('Failed to clear working memory:', e);
        } finally {
            setIsClearing(false);
        }
    };

    if (isLoading) {
        return (
            <div className={styles.loadingContainer}>
                <Loader2 className={styles.spinner} size={24} />
                <span data-i18n="components.memory.WorkingMemoryViewer.text001">{uiText("components.memory.WorkingMemoryViewer.text001")}</span>
            </div>
        );
    }

    return (
        <div className={styles.container}>
            <div className={styles.toolbar}>
                <div className={styles.toolbarLeft}>
                    <span className={styles.countBadge}>
                        {items.length} / {maxCapacity}
                    </span>
                    <button data-i18n="components.memory.WorkingMemoryViewer.text002" className={styles.refreshButton} onClick={fetchItems} title={uiText("components.memory.WorkingMemoryViewer.text002")}>
                        <RefreshCw size={14} />
                    </button>
                </div>
                <div className={styles.toolbarRight}>
                    <button data-i18n="components.memory.WorkingMemoryViewer.text003"
                        className={styles.clearButton}
                        onClick={clearAll}
                        disabled={items.length === 0 || isClearing}
                    >
                        {isClearing ? (
                            <Loader2 className={styles.spinner} size={14} />
                        ) : (
                            <Trash2 size={14} />
                        )}{uiText("components.memory.WorkingMemoryViewer.text003")}</button>
                </div>
            </div>

            {items.length === 0 ? (
                <div className={styles.emptyContainer}>
                    <Brain size={48} className={styles.emptyIcon} />
                    <p data-i18n="components.memory.WorkingMemoryViewer.text004">{uiText("components.memory.WorkingMemoryViewer.text004")}</p>
                    <p data-i18n="components.memory.WorkingMemoryViewer.text005" className={styles.emptyHint}>{uiText("components.memory.WorkingMemoryViewer.text005")}</p>
                </div>
            ) : (
                <div className={styles.itemsList}>
                    {items.map((item, index) => (
                        <div key={`${item.id}-${index}`} className={styles.itemRow}>
                            <div className={styles.itemInfo}>
                                <div className={styles.itemHeader}>
                                    <span className={`${styles.typeBadge} ${item.type === 'chronicle' ? styles.typeBadgeChronicle : styles.typeBadgeMemopedia}`}>
                                        {item.type === 'chronicle' ? 'Chronicle' : 'Memopedia'}
                                    </span>
                                    <span className={styles.itemTitle}>{item.title}</span>
                                </div>
                                <div className={styles.itemMeta}>
                                    <span className={styles.itemId} title={item.id}>
                                        {item.id.length > 24 ? item.id.slice(0, 24) + '...' : item.id}
                                    </span>
                                    {item.recalled_at && (
                                        <span className={styles.itemTime}>
                                            {formatTimestamp(item.recalled_at)}
                                        </span>
                                    )}
                                </div>
                            </div>
                            <button data-i18n="components.memory.WorkingMemoryViewer.text006"
                                className={styles.removeButton}
                                onClick={() => removeItem(item.id)}
                                disabled={removingId === item.id}
                                title={uiText("components.memory.WorkingMemoryViewer.text006")}
                            >
                                {removingId === item.id ? (
                                    <Loader2 className={styles.spinner} size={14} />
                                ) : (
                                    <XCircle size={16} />
                                )}
                            </button>
                        </div>
                    ))}
                </div>
            )}

            <div data-i18n="components.memory.WorkingMemoryViewer.text007 components.memory.WorkingMemoryViewer.text008" className={styles.infoBar}>{uiText("components.memory.WorkingMemoryViewer.text007")}{maxCapacity}{uiText("components.memory.WorkingMemoryViewer.text008")}</div>
        </div>
    );
}
